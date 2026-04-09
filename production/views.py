from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse
from .models import JobOrder, ExtrusionLog, CuttingLog, PackingLog, RawMaterial
from django.db.models import F, Q
from django.db import transaction
from decimal import Decimal
from .models import MaterialUsageLog, MaterialAllocation

# -----------------------------------------------------------------------------
# MATERIAL USAGE SUBMISSION
# -----------------------------------------------------------------------------
def submit_material_usage(request):
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        material_id = request.POST.get('material_id')
        
        try:
            amount_kg = float(request.POST.get('amount_kg'))
        except ValueError:
            return render(request, 'production/partials/error_message.html', {'error': "Invalid amount."})

        if amount_kg <= 0:
            return render(request, 'production/partials/error_message.html', {'error': "Amount must be greater than zero."})

        job_order = get_object_or_404(JobOrder, id=jo_id)
        material = get_object_or_404(RawMaterial, id=material_id)

        usage_log = MaterialUsageLog.objects.create(
            job_order=job_order,
            material=material,
            amount_kg=amount_kg,
            operator_name="Extrusion Op"
        )
        
        # Check for overuse warning
        allocation = MaterialAllocation.objects.filter(job_order=job_order, material=material).first()
        if allocation and allocation.is_overused:
            warning_msg = f"⚠️ WARNING: You have exceeded the allocated limit for {material.name}!"
            return render(request, 'production/partials/error_message.html', {'error': warning_msg})
            
        return HttpResponse(f"<div style='color: green; font-weight: bold;'>Successfully logged {amount_kg} KG of {material.name}.</div>")

def operator_dashboard(request):
    job_orders = JobOrder.objects.all()
    return render(request, 'production/dashboard.html', {'job_orders': job_orders})

from .models import ExtrusionSession, SessionMaterial # Ensure these are imported

# -----------------------------------------------------------------------------
# STATEFUL EXTRUSION SESSIONS
# -----------------------------------------------------------------------------
def load_machine_state(request, machine_no=None):
    """Checks if a machine is currently running a job or is idle."""
    
    # 1. If it wasn't called internally with an argument, grab it from the HTMX GET request
    if not machine_no:
        machine_no = request.GET.get('machine_no')
        
    # 2. If it is still empty (e.g., they selected the default "-- Select --" option)
    if not machine_no:
        return HttpResponse("<p style='color: var(--text-muted); font-weight: bold; text-transform: uppercase;'>Awaiting Machine Selection...</p>")

    # 3. Proceed with the original logic
    active_session = ExtrusionSession.objects.filter(machine_no=machine_no, status='ACTIVE').first()
    
    if active_session:
        return render(request, 'production/partials/active_run_ui.html', {'session': active_session})
    else:
        job_orders = JobOrder.objects.filter(is_completed=False, order_quantity_kg__gt=0).order_by('-id')[:20]
        raw_materials = RawMaterial.objects.all()
        return render(request, 'production/partials/start_session_ui.html', {
            'machine_no': machine_no, 
            'job_orders': job_orders,
            'raw_materials': raw_materials
        })
def start_extrusion_session(request):
    """Locks the machine, reserves the material, and starts the job."""
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        machine_no = request.POST.get('machine_no')
        shift = request.POST.get('shift')
        target_amount = float(request.POST.get('target_amount'))
        
        job_order = get_object_or_404(JobOrder, id=jo_id)
        
        with transaction.atomic():
            # Create the Active Session
            session = ExtrusionSession.objects.create(
                job_order=job_order, machine_no=machine_no, shift=shift, target_amount_kg=target_amount
            )
            
            # Process reserved materials (Assuming frontend sends material_id and reserved_amount arrays)
            material_ids = request.POST.getlist('material_ids')
            reserved_amounts = request.POST.getlist('reserved_amounts')
            
            for mat_id, amount in zip(material_ids, reserved_amounts):
                if float(amount) > 0:
                    mat = RawMaterial.objects.select_for_update().get(id=mat_id)
                    mat.current_stock_kg -= Decimal(str(amount)) # Deduct from warehouse immediately
                    mat.save()
                    
                    SessionMaterial.objects.create(
                        session=session, material=mat, reserved_kg=amount
                    )
                    
        return load_machine_state(request, machine_no)

def log_session_roll(request):
    """Operator logs a roll to their currently active session."""
    if request.method == "POST":
        session_id = request.POST.get('session_id')
        session = get_object_or_404(ExtrusionSession, id=session_id)
        
        try:
            roll_weight = float(request.POST.get('roll_weight'))
            wastage = float(request.POST.get('wastage') or 0)
        except ValueError:
            return HttpResponse("<div class='error'>Invalid numbers.</div>")

        ExtrusionLog.objects.create(session=session, roll_weight_kg=roll_weight, wastage_kg=wastage)
        
        session.refresh_from_db() # Refresh to see if auto-stop triggered
        if session.status == 'COMPLETED':
            return HttpResponse("<div class='success'>Target Reached! Session Auto-Completed.</div>")
            
        return load_machine_state(request, session.machine_no)

def stop_extrusion_session(request, session_id):
    """Operator manually terminates the job early."""
    session = get_object_or_404(ExtrusionSession, id=session_id)
    session.stop_session()
    return load_machine_state(request, session.machine_no)

# -----------------------------------------------------------------------------
# CUTTING SUBMISSION
# -----------------------------------------------------------------------------
def submit_cutting(request):
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        try:
            output_kg = float(request.POST.get('output_kg'))
            wastage = float(request.POST.get('wastage') or 0)
        except ValueError:
            return render(request, 'production/partials/error_message.html', {'error': "Invalid input. Please enter numbers only."})

        if output_kg <= 0 or wastage < 0:
            return render(request, 'production/partials/error_message.html', {'error': "Weights cannot be zero or negative."})
        if output_kg > 1000: 
            return render(request, 'production/partials/error_message.html', {'error': f"An output of {output_kg}kg seems too high for a single entry."})

        job_order = get_object_or_404(JobOrder, id=jo_id)
        
        # BUG FIX 1: Safely cast Decimals to floats to prevent Python TypeErrors during multiplication
        if (float(job_order.total_cut_kg) + output_kg) > (float(job_order.total_extruded_kg) * 1.05):
            return render(request, 'production/partials/error_message.html', {'error': "Cannot cut more material than the Extrusion department has produced!"})

        CuttingLog.objects.create(
            job_order=job_order,
            machine_no=request.POST.get('machine'),
            shift=request.POST.get('shift'),
            output_kg=output_kg,
            wastage_kg=wastage,
            operator_name="Cutting Op"
        )
        job_order.refresh_from_db()
        
        return render(request, 'production/partials/cutting_success.html', {'jo': job_order})

# -----------------------------------------------------------------------------
# PACKING SUBMISSION
# -----------------------------------------------------------------------------
def submit_packing(request):
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        try:
            packing_size = float(request.POST.get('packing_size'))
            quantity = int(request.POST.get('quantity'))
        except ValueError:
            return render(request, 'production/partials/error_message.html', {'error': "Invalid input. Ensure quantity is a whole number."})

        if packing_size <= 0 or quantity <= 0:
            return render(request, 'production/partials/error_message.html', {'error': "Values must be greater than zero."})

        total_weight_submitting = packing_size * quantity
        job_order = get_object_or_404(JobOrder, id=jo_id)

        # BUG FIX 2: Ensure operators cannot pack more than the factory has physically produced
        available_to_pack = float(job_order.total_cut_kg) if float(job_order.total_cut_kg) > 0 else float(job_order.total_extruded_kg)
        if (float(job_order.total_packed_kg) + total_weight_submitting) > (available_to_pack * 1.05):
            return render(request, 'production/partials/error_message.html', {'error': "Cannot pack more material than has been produced/cut!"})

        PackingLog.objects.create(
            job_order=job_order,
            packing_size_kg=packing_size,
            quantity_packed=quantity,
            operator_name="Packing Op"
        )
        job_order.refresh_from_db()
        
        return render(request, 'production/partials/packing_success.html', {'jo': job_order})

# -----------------------------------------------------------------------------
# HTMX FORM FETCHING & SEARCHING
# -----------------------------------------------------------------------------
def get_extrusion_form(request):
    job_orders = JobOrder.objects.filter(total_extruded_kg__lt=F('order_quantity_kg')).order_by('-id')[:20]
    return render(request, 'production/partials/extrusion_form.html', {'job_orders': job_orders})

def get_cutting_form(request):
    job_orders = JobOrder.objects.filter(total_extruded_kg__gt=0).order_by('-id')[:20]
    # Add 'dept': 'cutting' to the context here
    return render(request, 'production/partials/cutting_form.html', {'job_orders': job_orders, 'dept': 'cutting'})

def get_packing_form(request):
    job_orders = JobOrder.objects.filter(total_extruded_kg__gt=0).order_by('-id')[:20]
    # Add 'dept': 'packing' to the context here
    return render(request, 'production/partials/packing_form.html', {'job_orders': job_orders, 'dept': 'packing'})

def search_jobs(request):
    query = request.GET.get('q', '')
    dept = request.GET.get('dept', '') 
    
    jobs = JobOrder.objects.filter(order_quantity_kg__gt=0)
    
    if dept == 'extrusion':
        jobs = jobs.filter(total_extruded_kg__lt=F('order_quantity_kg'))
    elif dept in ['cutting', 'packing']:
        jobs = jobs.filter(total_extruded_kg__gt=0)
        
    if query:
        jobs = jobs.filter(Q(jo_number__icontains=query) | Q(customer__icontains=query))
        
    # Crucial Fix: Pass 'dept' explicitly into the radio list context
    return render(request, 'production/partials/job_radio_list.html', {'job_orders': jobs[:20], 'dept': dept})

# -----------------------------------------------------------------------------
# DASHBOARD & TOWER LOGIC
# -----------------------------------------------------------------------------
def control_tower(request):
    # Fetch physical stock warnings
    low_stock_materials = RawMaterial.objects.filter(current_stock_kg__lte=F('reorder_point_kg'))
    
    # Fetch Hypothetical Stock (Shortfalls) that need purchasing
    purchasing_shortfalls = MaterialAllocation.objects.filter(shortfall_kg__gt=0, job_order__is_completed=False)
    
    # Dashboard stats
    active_jobs = JobOrder.objects.filter(is_completed=False).order_by('-id')[:10]
    
    context = {
        'low_stock_materials': low_stock_materials,
        'purchasing_shortfalls': purchasing_shortfalls,
        'active_jobs': active_jobs,
    }
    if request.htmx:
        return render(request, 'production/partials/tower_content.html', context)
    return render(request, 'production/control_tower.html', context)

def get_job_specs(request, jo_id):
    job_order = get_object_or_404(JobOrder, id=jo_id)
    # Default to cutting if nothing is passed, just to be safe
    dept = request.GET.get('dept', 'cutting') 
    return render(request, 'production/partials/job_spec_card.html', {'jo': job_order, 'dept': dept})