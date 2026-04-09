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
        
        if not jo_id or not material_id:
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: Please select both a Job Order and a Material.</div>")

        try:
            amount_kg = float(request.POST.get('amount_kg'))
        except ValueError:
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: Invalid amount. Please enter numbers only.</div>")

        if amount_kg <= 0:
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: Amount must be greater than zero.</div>")

        material = get_object_or_404(RawMaterial, id=material_id)
        job_order = get_object_or_404(JobOrder, id=jo_id)

        # Ensure the job is actually active
        if job_order.is_completed: # (Or if job_order.status == 'CANCELLED', depending on your model)
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: This Job Order is already closed or completed. You cannot log new data against it.</div>")

        # NEW CHECK: Prevent pulling phantom stock from the warehouse
        if amount_kg > float(material.current_stock_kg):
            return HttpResponse(f"<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: Insufficient stock. You requested {amount_kg}kg, but only {material.current_stock_kg}kg of {material.name} is available.</div>")

        usage_log = MaterialUsageLog.objects.create(
            job_order=job_order,
            material=material,
            amount_kg=amount_kg,
            operator_name="Extrusion Op"
        )
        
        # Deduct the stock globally (assuming you want immediate inventory deduction here)
        material.current_stock_kg -= Decimal(str(amount_kg))
        material.save()
        
        # Check for overuse warning against the job's theoretical allocation
        allocation = MaterialAllocation.objects.filter(job_order=job_order, material=material).first()
        if allocation and allocation.is_overused:
            warning_msg = f"⚠️ WARNING: Material logged successfully, but you have now exceeded the allocated formula limit for {material.name}!"
            return HttpResponse(f"<div style='padding: 15px; background: #ffc107; color: #333; border-radius: 4px; font-weight: bold; text-align: center;'>{warning_msg}</div>")
            
        return HttpResponse(f"<div style='padding: 15px; background: #28a745; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Successfully logged {amount_kg}kg of {material.name}.</div>")

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

        # Ensure the job is actually active
        if job_order.is_completed: # (Or if job_order.status == 'CANCELLED', depending on your model)
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: This Job Order is already closed or completed. You cannot log new data against it.</div>")
        
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

        if not session_id:
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: No active session found.</div>")
            
        session = get_object_or_404(ExtrusionSession, id=session_id)

        if session.status != 'ACTIVE':
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: This machine session is no longer active. Please refresh your dashboard.</div>")
        
        try:
            roll_weight = float(request.POST.get('roll_weight'))
            wastage = float(request.POST.get('wastage') or 0)
        except ValueError:
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: Invalid input. Ensure weights are numeric.</div>")

        # 1. Base Logic Checks
        if roll_weight <= 0:
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: Roll weight must be strictly greater than zero.</div>")
        
        # 2. Physical Machine Limits (Adjust 500 to match your actual factory winder limits)
        if roll_weight > 500: 
            return HttpResponse(f"<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: {roll_weight}kg exceeds maximum physical roll capacity. Check for typos.</div>")
            
        # 3. Wastage Logic
        if wastage < 0 or wastage > roll_weight:
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: Wastage cannot be negative or greater than the total roll weight itself.</div>")

        # Save the log
        ExtrusionLog.objects.create(session=session, roll_weight_kg=roll_weight, wastage_kg=wastage)
        
        # Check if the target was reached to trigger auto-completion
        session.refresh_from_db() 
        if session.status == 'COMPLETED':
            # Return a special completion message, calling load_machine_state to reset the UI
            return HttpResponse("<div style='padding: 15px; background: #28a745; color: white; border-radius: 4px; font-weight: bold; text-align: center; margin-bottom: 15px;'>Target Reached! Session Auto-Completed.</div>" + load_machine_state(request, session.machine_no).content.decode('utf-8'))
            
        # Standard successful reload of the active session UI
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
        
        # 1. Did they actually select a job from the list?
        if not jo_id:
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: Please select an active job from the list first.</div>")

        # 2. Are the inputs valid numbers?
        try:
            output_kg = float(request.POST.get('output_kg'))
            wastage = float(request.POST.get('wastage') or 0)
        except ValueError:
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: Invalid input. Please enter numbers only.</div>")

        # 3. Are the numbers positive?
        if output_kg <= 0 or wastage < 0:
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: Weights cannot be zero or negative.</div>")
            
        # 4. Is the output absurdly high for a single shift?
        if output_kg > 2000: 
            return HttpResponse(f"<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: {output_kg}kg exceeds the maximum allowed limit for a single entry. Check for typos.</div>")

        job_order = get_object_or_404(JobOrder, id=jo_id)

        # Ensure the job is actually active
        if job_order.is_completed: # (Or if job_order.status == 'CANCELLED', depending on your model)
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: This Job Order is already closed or completed. You cannot log new data against it.</div>")
        
        # 5. The Ultimate Gatekeeper: Can they physically cut this much?
        remaining_to_cut = float(job_order.total_extruded_kg) - float(job_order.total_cut_kg)
        
        if output_kg > (remaining_to_cut * 1.05): # 5% margin for scale discrepancies
            return HttpResponse(f"<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: Cannot log {output_kg}kg. Only {remaining_to_cut:.1f}kg remains from Extrusion.</div>")

        # 6. If it passes all checks, log it to the database
        CuttingLog.objects.create(
            job_order=job_order,
            machine_no=request.POST.get('machine'),
            shift=request.POST.get('shift'),
            output_kg=output_kg,
            wastage_kg=wastage,
            operator_name="Cutting Op" # Update this later if you implement user accounts
        )
        job_order.refresh_from_db()
        
        # Return the success file which updates the text and the progress bar
        return render(request, 'production/partials/cutting_success.html', {'jo': job_order})

# -----------------------------------------------------------------------------
# PACKING SUBMISSION
# -----------------------------------------------------------------------------
def submit_packing(request):
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        
        # 1. Did they actually select a job?
        if not jo_id:
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: Please select a job from the list first.</div>")

        # 2. Are the inputs valid numbers?
        try:
            packing_size = float(request.POST.get('packing_size'))
            quantity = int(request.POST.get('quantity'))
        except ValueError:
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: Invalid input. Ensure quantity is a whole number.</div>")

        # 3. Are they logging real amounts?
        if packing_size <= 0 or quantity <= 0:
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: Packing size and quantity must be greater than zero.</div>")

        total_weight_submitting = packing_size * quantity
        job_order = get_object_or_404(JobOrder, id=jo_id)

        # Ensure the job is actually active
        if job_order.is_completed: # (Or if job_order.status == 'CANCELLED', depending on your model)
            return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: This Job Order is already closed or completed. You cannot log new data against it.</div>")

        # 4. The Ultimate Gatekeeper: Have they cut enough material to pack this much?
        remaining_to_pack = float(job_order.total_cut_kg) - float(job_order.total_packed_kg)

        if total_weight_submitting > (remaining_to_pack * 1.05): # 5% margin
            return HttpResponse(f"<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: Attempting to pack {total_weight_submitting:.1f}kg, but only {remaining_to_pack:.1f}kg has been cut and is available.</div>")

        # 5. If it passes all checks, log it to the database
        PackingLog.objects.create(
            job_order=job_order,
            packing_size_kg=packing_size,
            quantity_packed=quantity,
            operator_name="Packing Op"
        )
        job_order.refresh_from_db()

        # Check if the entire order is now fulfilled
        if float(job_order.total_packed_kg) >= float(job_order.order_quantity_kg):
            job_order.is_completed = True
            job_order.save()
            
            # Optional: You could return a different success template here that says "JOB COMPLETE!" 
            # and removes the form entirely.
        
        # Return the success file which updates the text and the progress bar
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

    if job_order.is_completed: 
        return HttpResponse("<div style='padding: 15px; background: #dc3545; color: white; border-radius: 4px; font-weight: bold; text-align: center;'>Error: This Job Order is already closed or completed. You cannot log new data against it.</div>")
        
    # Safely catch both None and empty strings
    dept = request.GET.get('dept')
    if not dept:
        dept = 'cutting'
        
    return render(request, 'production/partials/job_spec_card.html', {'jo': job_order, 'dept': dept})