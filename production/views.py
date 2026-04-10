from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse
from .models import JobOrder, ExtrusionLog, CuttingLog, PackingLog, RawMaterial
from django.db.models import F, Q
from django.db import transaction
from decimal import Decimal
from .models import MaterialUsageLog, MaterialAllocation, MaterialCategory
import time
import uuid

def get_toast_popup(message, alert_type="error"):
    """Generates a self-removing floating banner for various alert types."""
    toast_id = f"toast-{int(time.time() * 1000)}"
    
    # Configure colours and icons dynamically
    themes = {
        "error": {"bg": "#dc3545", "text": "white", "icon": "⚠️"},
        "success": {"bg": "#28a745", "text": "white", "icon": "✅"},
        "warning": {"bg": "#ffc107", "text": "#333", "icon": "⚠️"}
    }
    theme = themes.get(alert_type, themes["error"])

    return f"""
    <div id="{toast_id}" style="position: fixed; top: 20px; right: 20px; z-index: 9999; padding: 15px 25px; background-color: {theme['bg']}; color: {theme['text']}; border-radius: 4px; font-size: 15px; font-weight: bold; box-shadow: 0 4px 10px rgba(0,0,0,0.3); transition: opacity 0.5s ease-out; pointer-events: none;">
        {theme['icon']} {message}
        <script>
            setTimeout(function() {{
                var banner = document.getElementById('{toast_id}');
                if (banner) {{
                    banner.style.opacity = '0';
                    setTimeout(function() {{ banner.remove(); }}, 500);
                }}
            }}, 4000);
        </script>
    </div>
    """

# -----------------------------------------------------------------------------
# MATERIAL USAGE SUBMISSION
# -----------------------------------------------------------------------------

def add_material_row(request):
    """Returns a fresh, empty material reservation row with a unique ID."""
    row_id = str(uuid.uuid4())[:8]
    categories = MaterialCategory.objects.all().order_by('name')
    return render(request, 'production/partials/material_row.html', {'row_id': row_id, 'categories': categories})

def get_materials_by_category(request):
    """Cascading dropdown fetcher."""
    cat_id = request.GET.get('category_id')
    materials = RawMaterial.objects.filter(category_id=cat_id).order_by('name')
    return render(request, 'production/partials/material_options.html', {'materials': materials})

def submit_material_usage(request):
    """Submits ad-hoc material usage, triggering relevant visual alerts."""
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        material_id = request.POST.get('material_id')
        
        if not jo_id or not material_id:
            return HttpResponse(get_toast_popup("Please select both a Job Order and a Material.", "error"))

        try:
            amount_kg = float(request.POST.get('amount_kg'))
        except ValueError:
            return HttpResponse(get_toast_popup("Invalid amount. Please enter numbers only.", "error"))

        if amount_kg <= 0:
            return HttpResponse(get_toast_popup("Amount must be greater than zero.", "error"))

        material = get_object_or_404(RawMaterial, id=material_id)
        job_order = get_object_or_404(JobOrder, id=jo_id)

        if job_order.is_completed:
            return HttpResponse(get_toast_popup("This Job Order is already closed or completed. You cannot log new data against it.", "error"))

        if amount_kg > float(material.current_stock_kg):
            return HttpResponse(get_toast_popup(f"Insufficient stock. You requested {amount_kg}kg, but only {material.current_stock_kg}kg of {material.name} is available.", "error"))

        usage_log = MaterialUsageLog.objects.create(
            job_order=job_order,
            material=material,
            amount_kg=amount_kg,
            operator_name="Extrusion Op"
        )
        
        material.current_stock_kg -= Decimal(str(amount_kg))
        material.save()
        
        allocation = MaterialAllocation.objects.filter(job_order=job_order, material=material).first()
        if allocation and allocation.is_overused:
            warning_msg = get_toast_popup(f"Material logged, but you have now exceeded the allocated formula limit for {material.name}!", "warning")
            return HttpResponse(warning_msg)
            
        success_msg = get_toast_popup(f"Successfully logged {amount_kg}kg of {material.name}.", "success")
        return HttpResponse(success_msg)
def operator_dashboard(request):
    job_orders = JobOrder.objects.all()
    return render(request, 'production/dashboard.html', {'job_orders': job_orders})

from .models import ExtrusionSession, SessionMaterial # Ensure these are imported

# -----------------------------------------------------------------------------
# STATEFUL EXTRUSION SESSIONS
# -----------------------------------------------------------------------------
def load_machine_state(request, machine_no=None):
    if not machine_no:
        machine_no = request.GET.get('machine_no')
        
    if not machine_no:
        return HttpResponse("<p style='color: var(--text-muted); font-weight: bold; text-transform: uppercase;'>Awaiting Machine Selection...</p>")

    active_session = ExtrusionSession.objects.filter(machine_no=machine_no, status='ACTIVE').first()
    
    if active_session:
        return render(request, 'production/partials/active_run_ui.html', {'session': active_session})
    else:
        job_orders = JobOrder.objects.filter(is_completed=False, order_quantity_kg__gt=0).order_by('-id')[:20]
        categories = MaterialCategory.objects.all().order_by('name')
        initial_row_id = str(uuid.uuid4())[:8]
        
        return render(request, 'production/partials/start_session_ui.html', {
            'machine_no': machine_no, 
            'job_orders': job_orders,
            'categories': categories,
            'initial_row_id': initial_row_id
        })
    
def start_extrusion_session(request):
    """Locks the machine, reserves the material, and starts the job."""
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        machine_no = request.POST.get('machine_no')
        shift = request.POST.get('shift')
        
        try:
            target_amount = float(request.POST.get('target_amount'))
        except ValueError:
            error_msg = get_toast_popup("Invalid target amount. Please check your numbers.")
            return HttpResponse(error_msg + load_machine_state(request, machine_no).content.decode('utf-8'))
            
        material_ids = request.POST.getlist('material_ids')
        reserved_amounts = request.POST.getlist('reserved_amounts')

        try:
            total_reserved = sum(float(amount) for amount in reserved_amounts if amount.strip())
        except ValueError:
            error_msg = get_toast_popup("Invalid material reservation amounts. Ensure they are numbers.")
            return HttpResponse(error_msg + load_machine_state(request, machine_no).content.decode('utf-8'))

        if total_reserved < target_amount:
            error_msg = get_toast_popup(f"Total reserved material ({total_reserved}kg) cannot be less than the target extrusion amount ({target_amount}kg).")
            return HttpResponse(error_msg + load_machine_state(request, machine_no).content.decode('utf-8'))
        
        job_order = get_object_or_404(JobOrder, id=jo_id)

        if job_order.is_completed: 
            error_msg = get_toast_popup("This Job Order is already closed or completed.")
            return HttpResponse(error_msg + load_machine_state(request, machine_no).content.decode('utf-8'))
        
        with transaction.atomic():
            session = ExtrusionSession.objects.create(
                job_order=job_order, machine_no=machine_no, shift=shift, target_amount_kg=target_amount
            )
            
            material_ids = request.POST.getlist('material_ids')
            reserved_amounts = request.POST.getlist('reserved_amounts')
            
            for mat_id, amount in zip(material_ids, reserved_amounts):
                # Ensure they actually selected a material and entered a positive amount
                if mat_id and amount.strip() and float(amount) > 0:
                    mat = RawMaterial.objects.select_for_update().get(id=mat_id)
                    
                    if float(amount) > float(mat.current_stock_kg):
                        error_msg = get_toast_popup(f"Insufficient stock. You attempted to reserve {amount}kg of {mat.name}, but only {mat.current_stock_kg}kg is available.")
                        return HttpResponse(error_msg + load_machine_state(request, machine_no).content.decode('utf-8'))
                        
                    mat.current_stock_kg -= Decimal(str(amount))
                    mat.save()
                    
                    SessionMaterial.objects.create(
                        session=session, material=mat, reserved_kg=amount
                    )
                    
        return load_machine_state(request, machine_no)
    
def log_session_roll(request):
    """Operator logs a roll to their currently active session."""
    if request.method == "POST":
        session_id = request.POST.get('session_id')

        # Helper to return an error banner and an empty state if we lose context
        def error_no_context(msg):
            return HttpResponse(get_toast_popup(msg, "error"))

        if not session_id:
            return error_no_context("No active session found.")
            
        session = get_object_or_404(ExtrusionSession, id=session_id)

        # Helper to return an error banner and reload the current machine state
        def reload_with_error(msg):
            return HttpResponse(get_toast_popup(msg, "error") + load_machine_state(request, session.machine_no).content.decode('utf-8'))

        if session.status != 'ACTIVE':
            return reload_with_error("This machine session is no longer active. Please refresh your dashboard.")
        
        try:
            roll_weight = float(request.POST.get('roll_weight'))
            wastage = float(request.POST.get('wastage') or 0)
        except ValueError:
            return reload_with_error("Invalid input. Ensure weights are numeric.")

        if roll_weight <= 0:
            return reload_with_error("Roll weight must be strictly greater than zero.")
        
        if roll_weight > 500: 
            return reload_with_error(f"{roll_weight}kg exceeds maximum physical roll capacity. Check for typos.")
            
        if wastage < 0 or wastage > roll_weight:
            return reload_with_error("Wastage cannot be negative or greater than the total roll weight itself.")

        # Save the log
        ExtrusionLog.objects.create(session=session, roll_weight_kg=roll_weight, wastage_kg=wastage)
        
        # Check if the target was reached to trigger auto-completion
        session.refresh_from_db() 
        if session.status == 'COMPLETED':
            success_msg = get_toast_popup("Target Reached! Session Auto-Completed.", "success")
            return HttpResponse(success_msg + load_machine_state(request, session.machine_no).content.decode('utf-8'))
            
        # Standard successful reload of the active session UI without an alert
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
        
        def reload_with_error(error_text):
            jobs = JobOrder.objects.filter(total_extruded_kg__gt=F('total_cut_kg')).order_by('-id')[:20]
            err_html = get_toast_popup(error_text)
            form_html = render(request, 'production/partials/cutting_form.html', {'job_orders': jobs, 'dept': 'cutting'}).content.decode('utf-8')
            return HttpResponse(err_html + form_html)

        if not jo_id:
            return reload_with_error("Please select an active job from the list first.")

        try:
            output_kg = float(request.POST.get('output_kg'))
            wastage = float(request.POST.get('wastage') or 0)
        except ValueError:
            return reload_with_error("Invalid input. Please enter numbers only.")

        if output_kg <= 0 or wastage < 0:
            return reload_with_error("Weights cannot be zero or negative.")
            
        if output_kg > 2000: 
            return reload_with_error(f"{output_kg}kg exceeds the maximum allowed limit for a single entry. Check for typos.")

        job_order = get_object_or_404(JobOrder, id=jo_id)

        if job_order.is_completed:
            return reload_with_error("This Job Order is already closed or completed.")
        
        remaining_to_cut = float(job_order.total_extruded_kg) - float(job_order.total_cut_kg)
        
        if output_kg > (remaining_to_cut * 1.05):
            return reload_with_error(f"Cannot log {output_kg}kg. Only {remaining_to_cut:.1f}kg remains from Extrusion.")

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
        
        def reload_with_error(error_text):
            jobs = JobOrder.objects.filter(total_cut_kg__gt=F('total_packed_kg')).order_by('-id')[:20]
            err_html = get_toast_popup(error_text)
            form_html = render(request, 'production/partials/packing_form.html', {'job_orders': jobs, 'dept': 'packing'}).content.decode('utf-8')
            return HttpResponse(err_html + form_html)

        if not jo_id:
            return reload_with_error("Please select a job from the list first.")

        try:
            packing_size = float(request.POST.get('packing_size'))
            quantity = int(request.POST.get('quantity'))
        except ValueError:
            return reload_with_error("Invalid input. Ensure quantity is a whole number.")

        if packing_size <= 0 or quantity <= 0:
            return reload_with_error("Packing size and quantity must be greater than zero.")

        total_weight_submitting = packing_size * quantity
        job_order = get_object_or_404(JobOrder, id=jo_id)

        if job_order.is_completed:
            return reload_with_error("This Job Order is already closed or completed.")

        remaining_to_pack = float(job_order.total_cut_kg) - float(job_order.total_packed_kg)

        if total_weight_submitting > (remaining_to_pack * 1.05):
            return reload_with_error(f"Attempting to pack {total_weight_submitting:.1f}kg, but only {remaining_to_pack:.1f}kg is available.")

        PackingLog.objects.create(
            job_order=job_order,
            packing_size_kg=packing_size,
            quantity_packed=quantity,
            operator_name="Packing Op"
        )
        job_order.refresh_from_db()

        if float(job_order.total_packed_kg) >= float(job_order.order_quantity_kg):
            job_order.is_completed = True
            job_order.save()
            
        return render(request, 'production/partials/packing_success.html', {'jo': job_order}) 
# -----------------------------------------------------------------------------
# HTMX FORM FETCHING & SEARCHING
# -----------------------------------------------------------------------------
def get_extrusion_form(request):
    job_orders = JobOrder.objects.filter(total_extruded_kg__lt=F('order_quantity_kg')).order_by('-id')[:20]
    return render(request, 'production/partials/extrusion_form.html', {'job_orders': job_orders})

def get_cutting_form(request):
    # Only show jobs where extruded material exists that HAS NOT yet been cut
    job_orders = JobOrder.objects.filter(total_extruded_kg__gt=F('total_cut_kg')).order_by('-id')[:20]
    return render(request, 'production/partials/cutting_form.html', {'job_orders': job_orders, 'dept': 'cutting'})

def get_packing_form(request):
    # Only show jobs where cut material exists that HAS NOT yet been packed
    job_orders = JobOrder.objects.filter(total_cut_kg__gt=F('total_packed_kg')).order_by('-id')[:20]
    return render(request, 'production/partials/packing_form.html', {'job_orders': job_orders, 'dept': 'packing'})

def search_jobs(request):
    query = request.GET.get('q', '')
    dept = request.GET.get('dept', '') 
    
    jobs = JobOrder.objects.filter(order_quantity_kg__gt=0)
    
    # Apply the strict stage-completion filters to the search bar as well
    if dept == 'extrusion':
        jobs = jobs.filter(total_extruded_kg__lt=F('order_quantity_kg'))
    elif dept == 'cutting':
        jobs = jobs.filter(total_extruded_kg__gt=F('total_cut_kg'))
    elif dept == 'packing':
        jobs = jobs.filter(total_cut_kg__gt=F('total_packed_kg'))
        
    if query:
        jobs = jobs.filter(Q(jo_number__icontains=query) | Q(customer__icontains=query))
        
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
        return HttpResponse(get_toast_popup("This Job Order is already closed or completed. You cannot view active specs for it.", "error"))
        
    dept = request.GET.get('dept')
    
    # If the frontend didn't explicitly specify a department, infer it dynamically
    if not dept:
        if float(job_order.total_extruded_kg) < float(job_order.order_quantity_kg):
            dept = 'extrusion'
        elif float(job_order.total_cut_kg) < float(job_order.total_extruded_kg):
            dept = 'cutting'
        else:
            dept = 'packing'
            
    return render(request, 'production/partials/job_spec_card.html', {'jo': job_order, 'dept': dept})