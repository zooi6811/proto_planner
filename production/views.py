from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse
from django.db.models import Sum, F, Q, DecimalField
from django.db.models.functions import Coalesce
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils.html import escape
from decimal import Decimal, InvalidOperation
from .models import (JobOrder, ExtrusionLog, CuttingLog, PackingLog, RawMaterial, 
    MaterialUsageLog, MaterialAllocation, MaterialCategory, UserProfile, CuttingSession,
    ExtrusionSession, SessionMaterial)
from django.utils import timezone
import uuid
from datetime import timedelta
from django.utils.html import escape
from django.contrib.auth import authenticate, login as django_login
from django.contrib import messages
from .models import UserProfile
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import logout as django_logout
from django.contrib.auth.models import User
from django.views.decorators.cache import never_cache
import json
from django.template.loader import render_to_string

def gateway_login(request):
    if request.user.is_authenticated:
        if hasattr(request.user, 'profile') and request.user.profile.role == 'STAFF':
            return redirect('control_tower')
        return redirect('dashboard')

    if request.method == 'POST':
        login_type = request.POST.get('login_type')

        if login_type == 'operator':
            pin = request.POST.get('pin_code')
            try:
                profile = UserProfile.objects.get(pin_code=pin, role='OPERATOR')
                django_login(request, profile.user)
                return redirect('dashboard')
            except UserProfile.DoesNotExist:
                messages.error(request, "Invalid Operator PIN.")

        elif login_type == 'staff':
            username = request.POST.get('username')
            password = request.POST.get('password')
            user = authenticate(request, username=username, password=password)
            
            if user is not None:
                django_login(request, user)
                if hasattr(user, 'profile') and user.profile.role == 'STAFF':
                    return redirect('control_tower')
                return redirect('dashboard')
            else:
                messages.error(request, "Invalid credentials or unauthorised access.")

    return render(request, 'production/login.html')

# @user_passes_test(lambda u: u.is_staff)
def register_user(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        role = request.POST.get('role')
        pin_code = request.POST.get('pin_code')

        if role == 'OPERATOR' and (not pin_code or not pin_code.isdigit() or len(pin_code) != 4):
            messages.error(request, "Operators must have a valid 4-digit PIN.")
        elif User.objects.filter(username=username).exists():
            messages.error(request, "Username already exists.")
        elif role == 'OPERATOR' and UserProfile.objects.filter(pin_code=pin_code).exists():
            messages.error(request, "That PIN is already assigned to another operator.")
        else:
            with transaction.atomic():
                user = User.objects.create_user(username=username, password=password)
                UserProfile.objects.create(user=user, role=role, pin_code=pin_code if role == 'OPERATOR' else None)
            messages.success(request, f"User {username} successfully registered.")
            return redirect('control_tower')
            
    return render(request, 'production/register.html')

def render_toast(message, alert_type="error", use_oob=True):
    """Renders the toast notification via a proper Django template."""
    themes = {
        "error": {"bg": "#dc3545", "text": "white", "icon": "⚠️"},
        "success": {"bg": "#28a745", "text": "white", "icon": "✅"},
        "warning": {"bg": "#ffc107", "text": "#333", "icon": "⚠️"}
    }
    theme = themes.get(alert_type, themes["error"])
    return render_to_string('production/partials/toast.html', {
        'message': escape(str(message)),
        'theme': theme,
        'use_oob': use_oob
    })

def trigger_refresh(response, url, target):
    """Attaches a robust HX-Trigger payload to drive frontend refreshes without inline scripts."""
    trigger_data = {
        "softRefresh": {
            "url": url,
            "target": target
        }
    }
    response['HX-Trigger'] = json.dumps(trigger_data)
    return response

def trigger_packing_refresh(response, job_id, is_completed):
    """Sends a specialised event payload to drive the packing UI updates."""
    trigger_data = {
        "packingRefresh": {
            "jobId": job_id,
            "isCompleted": is_completed
        }
    }
    response['HX-Trigger'] = json.dumps(trigger_data)
    return response

def parse_decimal(value):
    """Safely coerces form inputs into precise Decimals, bypassing TypeError crashes."""
    if value is None or str(value).strip() == '':
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    
def has_logging_permission(user):
    """Checks if a user has the authority to log production data."""
    if not user.is_authenticated:
        return False
    
    # Always allow standard Django backend admins and superusers
    if user.is_staff or user.is_superuser:
        return True
        
    # ONLY allow dedicated factory floor operators (Block the custom 'STAFF' viewer role)
    if hasattr(user, 'profile') and user.profile.role == 'OPERATOR':
        return True
    
    return False

# -----------------------------------------------------------------------------
# MATERIAL USAGE SUBMISSION
# -----------------------------------------------------------------------------

def add_material_row(request):
    """Returns a fresh, empty material reservation row with a unique ID."""
    try:
        row_id = str(uuid.uuid4())[:8]
        categories = MaterialCategory.objects.all().order_by('name')
        return render(request, 'production/partials/material_row.html', {'row_id': row_id, 'categories': categories})
    except Exception as e:
        # If the backend crashes, render the error visually so we aren't guessing
        return HttpResponse(f'<div style="color:var(--status-red); padding:10px; border:1px solid red;">Backend Error: {str(e)}</div>')
    
def get_materials_by_category(request):
    """Cascading dropdown fetcher completely immune to HTMX list serialization traps."""
    try:
        cat_id = None
        
        # Smart extraction: Find the exact dynamic key triggering the request
        # (e.g., ignoring everything else and hunting only for 'category_ABC123')
        for key, value in request.GET.items():
            if key.startswith('category_'):
                cat_id = value
                break
                
        # Fallback for standard requests
        if not cat_id:
            cat_id = request.GET.get('category_id')

        if not cat_id or str(cat_id).strip() == '':
            return HttpResponse('<option value="" selected disabled>-- Awaiting Category --</option>')

        # Pre-validate as integer to prevent ORM ValidationErrors
        clean_id = int(str(cat_id).strip())
        materials = RawMaterial.objects.filter(category_id=clean_id).order_by('name')
        return render(request, 'production/partials/material_options.html', {'materials': materials})

    except (ValueError, TypeError, Exception):
        # Catches rogue data payloads gracefully
        return HttpResponse('<option value="" selected disabled>-- Selection Error --</option>')

def submit_material_usage(request):
    """Submits ad-hoc material usage, triggering relevant visual alerts."""
    if not has_logging_permission(request.user):
        return HttpResponse(render_toast("Unauthorised: Only Operators and Admins can log material usage.", "error"))
    
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        material_id = request.POST.get('material_id')
        
        if not jo_id or not material_id:
            return HttpResponse(render_toast("Please select both a Job Order and a Material.", "error"))

        amount_kg = parse_decimal(request.POST.get('amount_kg'))
        if amount_kg is None or amount_kg <= Decimal('0'):
            return HttpResponse(render_toast("Invalid amount. Please enter positive numbers only.", "error"))

        material = get_object_or_404(RawMaterial, id=material_id)
        job_order = get_object_or_404(JobOrder, id=jo_id)

        if job_order.is_completed:
            return HttpResponse(render_toast("This Job Order is already closed or completed. You cannot log new data against it.", "error"))

        if amount_kg > material.current_stock_kg:
            return HttpResponse(render_toast(f"Insufficient stock. You requested {amount_kg}kg, but only {material.current_stock_kg}kg of {material.name} is available.", "error"))

        operator = request.user.username if request.user.is_authenticated else "Unknown Operator"

        usage_log = MaterialUsageLog.objects.create(
            job_order=job_order,
            material=material,
            amount_kg=amount_kg,
            operator_name=operator
        )
        
        allocation = MaterialAllocation.objects.filter(job_order=job_order, material=material).first()
        if allocation and allocation.is_overused:
            warning_msg = render_toast(f"Material logged, but you have now exceeded the allocated formula limit for {material.name}!", "warning")
            return HttpResponse(warning_msg)
            
        success_msg = render_toast(f"Successfully logged {amount_kg}kg of {material.name}.", "success")
        return HttpResponse(success_msg)

@login_required(login_url='login')
def operator_dashboard(request):
    # The default tab is Extrusion, so we must load the properly sorted Extrusion queue initially
    job_orders = JobOrder.objects.filter(
        is_completed=False, 
        order_quantity_kg__gt=F('total_extruded_kg') - F('total_cutting_wastage_kg')
    ).order_by('queue_position', 'target_delivery_date', 'id')[:20]
    
    active_machines = ExtrusionSession.objects.filter(status='ACTIVE').values_list('machine_no', flat=True)
    
    return render(request, 'production/dashboard.html', {
        'job_orders': job_orders,
        'active_machines': list(active_machines)
    })

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
        queued_jobs = JobOrder.objects.filter(is_completed=False, order_quantity_kg__gt=0).exclude(id=active_session.job_order.id).order_by('queue_position')[:15]
        
        # --- NEW: Calculate Remaining Hopper Material ---
        active_material_name = "No Material Reserved"
        total_reserved = sum(sm.reserved_kg for sm in active_session.materials.all())
        total_consumed = active_session.total_output_kg + active_session.total_wastage_kg
        remaining_material_kg = max(Decimal('0.00'), total_reserved - total_consumed)
        
        first_mat = active_session.materials.first()
        if first_mat:
            active_material_name = first_mat.material.name

        return render(request, 'production/partials/active_run_ui.html', {
            'session': active_session,
            'queued_jobs': queued_jobs,
            'active_material_name': active_material_name,
            'remaining_material_kg': remaining_material_kg
        })
    else:
        # FIX: Ensure the priority queue ordering is applied to the machine workspace dropdown!
        job_orders = JobOrder.objects.filter(
            is_completed=False, 
            order_quantity_kg__gt=F('total_extruded_kg') - F('total_cutting_wastage_kg')
        ).order_by('queue_position', 'target_delivery_date', 'id')[:20]
        
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
    if not has_logging_permission(request.user):
        return HttpResponse(render_toast("Unauthorised: Only Operators and Admins can start sessions.", "error", use_oob=False))
    
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        machine_no = request.POST.get('machine_no')
        shift = request.POST.get('shift')
        
        def reload_with_error(msg):
            return HttpResponse(render_toast(msg, "error", use_oob=False))

        if not jo_id:
            return reload_with_error("Please select a Job Order from the list.")

        target_amount = parse_decimal(request.POST.get('target_amount'))
        if target_amount is None or target_amount <= Decimal('0'):
            return reload_with_error("Invalid target amount. Please check your numbers.")
            
        material_ids = request.POST.getlist('material_ids')
        reserved_amounts_raw = request.POST.getlist('reserved_amounts')
        reserved_amounts = []

        for amt in reserved_amounts_raw:
            if amt.strip():
                parsed = parse_decimal(amt)
                if parsed is None or parsed <= Decimal('0'):
                    return reload_with_error("Invalid material reservation amounts. Ensure they are numbers.")
                reserved_amounts.append(parsed)
            else:
                reserved_amounts.append(Decimal('0'))

        total_reserved = sum(reserved_amounts)

        if total_reserved < target_amount:
            return reload_with_error(f"Total reserved material ({total_reserved}kg) cannot be less than target ({target_amount}kg).")
        
        job_order = get_object_or_404(JobOrder, id=jo_id)

        if job_order.is_completed: 
            return reload_with_error("This Job Order is already closed or completed.")
        
        operator = request.user.username if request.user.is_authenticated else "Unknown Operator"

        try:
            with transaction.atomic():
                session = ExtrusionSession.objects.create(
                    job_order=job_order, machine_no=machine_no, shift=shift, 
                    target_amount_kg=target_amount, operator_name=operator
                )
                
                for mat_id, parsed_amount in zip(material_ids, reserved_amounts):
                    if mat_id and parsed_amount > Decimal('0'):
                        mat = RawMaterial.objects.select_for_update().get(id=mat_id)
                        
                        if parsed_amount > mat.current_stock_kg:
                            raise ValidationError(f"Insufficient stock. Attempted to reserve {parsed_amount}kg of {mat.name}, but only {mat.current_stock_kg}kg is available.")
                            
                        mat.current_stock_kg -= parsed_amount
                        mat.save()
                        
                        SessionMaterial.objects.create(
                            session=session, material=mat, reserved_kg=parsed_amount
                        )
        except ValidationError as e:
            error_msg = " ".join(e.messages) if hasattr(e, 'messages') else str(e)
            return reload_with_error(error_msg)

        success_toast = render_toast(f"Session locked in on Machine {machine_no}. Extrusion active.", "success", use_oob=False)
        response = HttpResponse(success_toast)
        
        return trigger_refresh(response, f"/load-machine-state/{machine_no}/", "#machine-workspace")
    
def log_session_roll(request):
    """Operator logs a roll to their currently active session."""
    if not has_logging_permission(request.user):
        return HttpResponse(render_toast("Unauthorised: Only Operators and Admins can log rolls.", "error", use_oob=False))
    
    if request.method == "POST":
        session_id = request.POST.get('session_id')

        def reload_with_error(msg):
            return HttpResponse(render_toast(msg, "error", use_oob=False))

        if not session_id:
            return reload_with_error("No active session found.")
            
        session = get_object_or_404(ExtrusionSession, id=session_id)

        if session.status != 'ACTIVE':
            return reload_with_error("This machine session is no longer active. Please start a new run.")
        
        roll_weight = parse_decimal(request.POST.get('roll_weight'))
        wastage = parse_decimal(request.POST.get('wastage')) or Decimal('0')
        
        if roll_weight is None or roll_weight <= Decimal('0'):
            return reload_with_error("Roll weight must be strictly greater than zero.")
        
        if roll_weight > Decimal('500'): 
            return reload_with_error(f"{roll_weight}kg exceeds maximum physical roll capacity. Check for typos.")
            
        if wastage < Decimal('0') or wastage > roll_weight:
            return reload_with_error("Wastage cannot be negative or greater than the total roll weight itself.")

        try:
            ExtrusionLog.objects.create(session=session, roll_weight_kg=roll_weight, wastage_kg=wastage)
        except ValidationError as e:
            error_msg = " ".join(e.messages) if hasattr(e, 'messages') else str(e)
            return reload_with_error(error_msg)
        
        session.refresh_from_db() 
        
        if session.status == 'COMPLETED':
            if session.total_output_kg >= session.target_amount_kg:
                success_msg = render_toast("Target Reached! Session Auto-Completed.", "success", use_oob=False)
            else:
                success_msg = render_toast("Material Depleted! Session Auto-Completed.", "warning", use_oob=False)
        else:
            success_msg = render_toast(f"Successfully logged {roll_weight}kg roll.", "success", use_oob=False)
            
        response = HttpResponse(success_msg)
        response['HX-Retarget'] = 'body'
        response['HX-Reswap'] = 'beforeend'
        
        return trigger_refresh(response, f"/load-machine-state/{session.machine_no}/", "#machine-workspace")
        
@transaction.atomic
def handover_extrusion_shift(request, session_id):
    """Protocol B: Seamlessly transfers an active session to a new operator."""
    if request.method == 'POST':
        session = get_object_or_404(ExtrusionSession, id=session_id, status='ACTIVE')
        new_operator = request.POST.get('operator_name')
        new_shift = request.POST.get('shift')
        
        session.operator_name = new_operator
        session.shift = new_shift
        session.save()
        
        success_toast = render_toast(f"Shift Handed Over to {new_operator}.", "success", use_oob=False)
        response = HttpResponse(success_toast)
        return trigger_refresh(response, f"/load-machine-state/{session.machine_no}/", "#machine-workspace")
    
@transaction.atomic
def rollover_extrusion_session(request, session_id):
    """Protocol C: Transfers remaining hopper material to a brand new Job Order."""
    if request.method == 'POST':
        old_session = get_object_or_404(ExtrusionSession, id=session_id, status='ACTIVE')
        next_job_id = request.POST.get('next_job_order_id')
        next_job = get_object_or_404(JobOrder, id=next_job_id)
        
        total_reserved = sum(sm.reserved_kg for sm in old_session.materials.all())
        total_consumed = old_session.total_output_kg + old_session.total_wastage_kg
        remaining_balance = max(Decimal('0.00'), total_reserved - total_consumed)
        
        new_session = ExtrusionSession.objects.create(
            machine_no=old_session.machine_no,
            job_order=next_job,
            operator_name=old_session.operator_name,
            shift=old_session.shift,
            status='ACTIVE',
            target_amount_kg=next_job.remaining_extrusion_kg 
        )
        
        primary_material_record = old_session.materials.first()
        if primary_material_record and remaining_balance > 0:
            SessionMaterial.objects.create(
                session=new_session,
                material=primary_material_record.material,
                reserved_kg=remaining_balance
            )
            
        old_session.transferred_to_next_job_kg = remaining_balance
        old_session.stop_session()
        
        success_toast = render_toast("Material Rolled Over to New Job.", "success", use_oob=False)
        response = HttpResponse(success_toast)
        return trigger_refresh(response, f"/load-machine-state/{new_session.machine_no}/", "#machine-workspace")

@transaction.atomic
def purge_and_close_session(request, session_id):
    """Protocol A: The Strict Reconciliation Gateway for emptying a machine."""
    if request.method == 'POST':
        session = get_object_or_404(ExtrusionSession, id=session_id, status='ACTIVE')
        
        returned_kg = Decimal(request.POST.get('returned_material_kg', '0'))
        final_waste = Decimal(request.POST.get('final_wastage_kg', '0'))
        force_discrepancy = request.POST.get('submit_with_discrepancy') == 'true'
        
        total_reserved = sum(sm.reserved_kg for sm in session.materials.all())
        total_consumed = session.total_output_kg + session.total_wastage_kg
        
        accounted_for = total_consumed + returned_kg + final_waste
        variance = total_reserved - accounted_for
        
        buffer = total_reserved * Decimal('0.01')
        
        if abs(variance) > buffer and not force_discrepancy:
            error = f"Discrepancy detected! You are missing {variance}kg of material. Please recount or flag a discrepancy."
            return HttpResponse(render_toast(error, "error", use_oob=False))
            
        session.returned_material_kg = returned_kg
        session.final_wastage_kg = final_waste
        
        if force_discrepancy:
            session.unaccounted_variance_kg = variance
            
        if returned_kg > 0:
            primary_material = session.materials.first()
            if primary_material:
                raw_mat = primary_material.material
                raw_mat.current_stock_kg += returned_kg
                raw_mat.save()
                
        session.stop_session()
        
        msg = "Session Closed with Discrepancy Flag." if force_discrepancy else "Session Cleanly Closed."
        toast_type = "warning" if force_discrepancy else "success"
        
        success_toast = render_toast(msg, toast_type, use_oob=False)
        response = HttpResponse(success_toast)
        return trigger_refresh(response, f"/load-machine-state/{session.machine_no}/", "#machine-workspace")
    
@login_required(login_url='login')
def complete_extrusion_session(request, session_id):
    """Operator successfully completes the extrusion job."""
    if request.method == 'POST':
        session = get_object_or_404(ExtrusionSession, id=session_id, status='ACTIVE')
        machine_no = session.machine_no
        
        session.status = 'COMPLETED'
        session.save()
        
        success_toast = render_toast(f"Extrusion on Machine {machine_no} completed successfully.", "success", use_oob=False)
        response = HttpResponse(success_toast)
        return trigger_refresh(response, f"/load-machine-state/{machine_no}/", "#machine-workspace")

def stop_extrusion_session(request, session_id):
    """Operator manually terminates the job early."""
    if not has_logging_permission(request.user):
        return HttpResponse(render_toast("Unauthorised: Only Operators and Admins can terminate sessions.", "error", use_oob=False))
    
    session = get_object_or_404(ExtrusionSession, id=session_id)
    
    machine_no = session.machine_no
    shift = session.shift
    
    session.stop_session()
    
    warning_toast = render_toast(f"Session on Machine {machine_no} terminated early.", "warning", use_oob=False)
    
    ajax_url = f"/load-machine-state/{machine_no}/?prefill_machine={machine_no}&prefill_shift={shift}"
    response = HttpResponse(warning_toast)
    return trigger_refresh(response, ajax_url, "#machine-workspace")

# -----------------------------------------------------------------------------
# CUTTING SESSIONS & SUBMISSION
# -----------------------------------------------------------------------------

def load_cutting_state(request, machine_no=None):
    if not machine_no:
        machine_no = request.GET.get('machine_no')
        
    if not machine_no:
        return HttpResponse("<p style='color: var(--text-muted); font-weight: bold; text-transform: uppercase;'>Awaiting Machine Selection...</p>")

    active_session = CuttingSession.objects.filter(machine_no=machine_no, status='ACTIVE').first()
    
    if active_session:
        total_consumed = active_session.total_output_kg + active_session.total_wastage_kg
        remaining_roll_kg = max(Decimal('0.00'), active_session.input_roll_weight_kg - total_consumed)
        
        return render(request, 'production/partials/active_cutting_ui.html', {
            'session': active_session,
            'remaining_roll_kg': remaining_roll_kg
        })
    else:
        # Load priority queue for cutting
        job_orders = JobOrder.objects.filter(
            is_completed=False,
            total_extruded_kg__gt=F('total_cut_kg') + F('total_cutting_wastage_kg')
        ).order_by('queue_position', 'target_delivery_date', 'id')[:20]
        
        return render(request, 'production/partials/start_cutting_ui.html', {
            'machine_no': machine_no, 
            'job_orders': job_orders,
            'dept': 'cutting'
        })

def start_cutting_session(request):
    """Locks the cutting machine and logs the input roll."""
    if not has_logging_permission(request.user):
        return HttpResponse(render_toast("Unauthorised: Only Operators can start sessions.", "error", use_oob=False))
        
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        machine_no = request.POST.get('machine_no')
        shift = request.POST.get('shift')
        
        def reload_with_error(msg):
            return HttpResponse(render_toast(msg, "error", use_oob=False))

        if not jo_id:
            return reload_with_error("Please select a Job Order from the list.")

        input_roll = parse_decimal(request.POST.get('input_roll_weight'))
        if input_roll is None or input_roll <= Decimal('0'):
            return reload_with_error("Invalid input roll weight. Must be greater than zero.")
            
        job_order = get_object_or_404(JobOrder, id=jo_id)

        if job_order.is_completed: 
            return reload_with_error("This Job Order is already completed.")
            
        remaining_extruded = job_order.total_extruded_kg - job_order.total_cut_kg - job_order.total_cutting_wastage_kg
        if input_roll > (remaining_extruded * Decimal('1.05')):
            return reload_with_error(f"Cannot mount {input_roll}kg roll. Only {remaining_extruded:.1f}kg remains from Extrusion.")
        
        operator = request.user.username if request.user.is_authenticated else "Unknown Operator"

        try:
            session = CuttingSession.objects.create(
                job_order=job_order, machine_no=machine_no, shift=shift, 
                input_roll_weight_kg=input_roll, operator_name=operator
            )
        except ValidationError as e:
            return reload_with_error(str(e))

        success_toast = render_toast(f"Session locked on Cut Machine {machine_no}.", "success", use_oob=False)
        response = HttpResponse(success_toast)
        return trigger_refresh(response, f"/load-cutting-state/{machine_no}/", "#cutting-workspace")

def log_cut_roll(request):
    """Logs the good output from the active cutting session."""
    if not has_logging_permission(request.user):
        return HttpResponse(render_toast("Unauthorised: Only Operators can log output.", "error", use_oob=False))
        
    if request.method == "POST":
        session_id = request.POST.get('session_id')

        def reload_with_error(msg):
            return HttpResponse(render_toast(msg, "error", use_oob=False))

        if not session_id:
            return reload_with_error("No active session found.")
            
        session = get_object_or_404(CuttingSession, id=session_id)

        if session.status != 'ACTIVE':
            return reload_with_error("This machine session is no longer active.")
        
        output_kg = parse_decimal(request.POST.get('output_kg'))
        
        if output_kg is None or output_kg <= Decimal('0'):
            return reload_with_error("Output weight must be strictly greater than zero.")

        try:
            CuttingLog.objects.create(session=session, output_kg=output_kg)
        except ValidationError as e:
            error_msg = " ".join(e.messages) if hasattr(e, 'messages') else str(e)
            return reload_with_error(error_msg)
        
        session.refresh_from_db() 
        
        if session.status == 'COMPLETED':
            success_msg = render_toast(f"Roll finished! Wastage automatically calculated as {session.total_wastage_kg}kg.", "success", use_oob=False)
        else:
            success_msg = render_toast(f"Successfully logged {output_kg}kg of cut goods.", "success", use_oob=False)
            
        response = HttpResponse(success_msg)
        return trigger_refresh(response, f"/load-cutting-state/{session.machine_no}/", "#cutting-workspace")
    
@login_required(login_url='login')
def complete_cutting_roll(request, session_id):
    if request.method == 'POST':
        session = get_object_or_404(CuttingSession, id=session_id, status='ACTIVE')
        machine_no = session.machine_no
        
        session.stop_session(calculate_wastage=True) 
        session.refresh_from_db()
        
        success_toast = render_toast(f"Roll completed! {session.total_wastage_kg}kg wastage logged.", "success", use_oob=False)
        response = HttpResponse(success_toast)
        return trigger_refresh(response, f"/load-cutting-state/{machine_no}/", "#cutting-workspace")

def stop_cutting_session(request, session_id):
    """Operator terminates the cutting session early."""
    if not has_logging_permission(request.user):
        return HttpResponse(render_toast("Unauthorised action.", "error", use_oob=False))
        
    session = get_object_or_404(CuttingSession, id=session_id)
    session.stop_session(calculate_wastage=False)
    
    warning_toast = render_toast(f"Cutting Session on Machine {session.machine_no} ended early. Wastage deferred.", "warning", use_oob=False)
    response = HttpResponse(warning_toast)
    return trigger_refresh(response, f"/load-cutting-state/{session.machine_no}/", "#cutting-workspace")

# -----------------------------------------------------------------------------
# PACKING SUBMISSION
# -----------------------------------------------------------------------------
def submit_packing(request):
    if not has_logging_permission(request.user):
        return HttpResponse(render_toast("Unauthorised: Only Operators and Admins can log packed goods.", "error", use_oob=False))
    
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        
        def reload_with_error(error_text):
            return HttpResponse(render_toast(error_text, "error", use_oob=False))

        if not jo_id:
            return reload_with_error("Please select a job from the list first.")

        packing_size = parse_decimal(request.POST.get('packing_size'))
        
        try:
            quantity = int(request.POST.get('quantity'))
        except (ValueError, TypeError):
            quantity = None

        if packing_size is None or packing_size <= Decimal('0') or quantity is None or quantity <= 0:
            return reload_with_error("Invalid input. Ensure packing size is a proper number and quantity is a whole number greater than zero.")

        total_weight_submitting = packing_size * Decimal(str(quantity))
        job_order = get_object_or_404(JobOrder, id=jo_id)

        if job_order.is_completed:
            return reload_with_error("This Job Order is already closed or completed.")

        remaining_to_pack = job_order.total_cut_kg - job_order.total_packed_kg

        if total_weight_submitting > (remaining_to_pack * Decimal('1.05')):
            return reload_with_error(f"Attempting to pack {total_weight_submitting:.1f}kg, but only {remaining_to_pack:.1f}kg is available.")

        operator = request.user.username if request.user.is_authenticated else "Unknown Operator"

        try:
            PackingLog.objects.create(
                job_order=job_order,
                packing_size_kg=packing_size,
                quantity_packed=quantity,
                operator_name=operator
            )
        except ValidationError as e:
            error_msg = " ".join(e.messages) if hasattr(e, 'messages') else str(e)
            return reload_with_error(error_msg)

        job_order.refresh_from_db()

        if job_order.total_packed_kg >= job_order.order_quantity_kg:
            job_order.complete_job()
            success_msg = f"Target Reached! Job {job_order.jo_number} is now fully packed and closed."
        else:
            success_msg = f"Successfully packed {total_weight_submitting}kg for {job_order.jo_number}."
            
        success_toast = render_toast(success_msg, "success", use_oob=False)
        is_target_reached = bool(job_order.is_completed)
        
        response = HttpResponse(success_toast)
        return trigger_packing_refresh(response, job_order.id, is_target_reached)
    
# -----------------------------------------------------------------------------
# HTMX FORM FETCHING & SEARCHING
# -----------------------------------------------------------------------------
@login_required(login_url='login')
def get_extrusion_form(request):
    # Notice the order_by now prioritises queue_position first!
    job_orders = JobOrder.objects.filter(
        is_completed=False, 
        order_quantity_kg__gt=F('total_extruded_kg') - F('total_cutting_wastage_kg')
    ).order_by('queue_position', 'target_delivery_date', 'id')[:20]
    
    active_machines = ExtrusionSession.objects.filter(status='ACTIVE').values_list('machine_no', flat=True)
    
    return render(request, 'production/partials/extrusion_form.html', {
        'job_orders': job_orders,
        'active_machines': list(active_machines)
    })

@login_required(login_url='login')
def get_cutting_form(request):
    active_machines = CuttingSession.objects.filter(status='ACTIVE').values_list('machine_no', flat=True)
    
    # 1. Catch the state parameters (defaults to an empty string if not found)
    prefill_machine = request.GET.get('prefill_machine', '')
    prefill_shift = request.GET.get('prefill_shift', '')
    
    # 2. Bundle them into your context dictionary
    context = {
        'active_machines': list(active_machines), 
        'dept': 'cutting',
        'prefill_machine': prefill_machine,
        'prefill_shift': prefill_shift,
    }
    
    return render(request, 'production/partials/cutting_form.html', context)

@login_required(login_url='login')
def get_packing_form(request):
    job_orders = JobOrder.objects.filter(
        is_completed=False,
        total_cut_kg__gt=F('total_packed_kg')
    ).order_by('queue_position', 'target_delivery_date', 'id')[:20]
    return render(request, 'production/partials/packing_form.html', {'job_orders': job_orders, 'dept': 'packing'})

def search_jobs(request):
    query = request.GET.get('q', '')
    dept = request.GET.get('dept', '') 
    
    jobs = JobOrder.objects.filter(is_completed=False, order_quantity_kg__gt=0)
    
    if dept == 'extrusion':
        jobs = jobs.filter(order_quantity_kg__gt=F('total_extruded_kg') - F('total_cutting_wastage_kg'))
    elif dept == 'cutting':
        jobs = jobs.filter(total_extruded_kg__gt=F('total_cut_kg') + F('total_cutting_wastage_kg'))
    elif dept == 'packing':
        jobs = jobs.filter(total_cut_kg__gt=F('total_packed_kg'))
        
    if query:
        jobs = jobs.filter(Q(jo_number__icontains=query) | Q(customer__icontains=query))
        
    # Ensure search results also respect the priority queue!
    jobs = jobs.order_by('queue_position', 'target_delivery_date', 'id')
        
    return render(request, 'production/partials/job_radio_list.html', {'job_orders': jobs[:20], 'dept': dept})

# -----------------------------------------------------------------------------
# DASHBOARD & TOWER LOGIC
# -----------------------------------------------------------------------------
@login_required(login_url='login')
@never_cache
def control_tower(request):
    if hasattr(request.user, 'profile') and request.user.profile.role == 'OPERATOR':
        return redirect('dashboard')
    
    timeframes = request.GET.getlist('timeframe')
    timeframe = timeframes[0] if timeframes else 'daily'
    
    expanded_param = request.GET.get('expanded', '')
    expanded_sections = expanded_param.split(',') if expanded_param else []
    
    job_tabs = request.GET.getlist('job_tab')
    job_tab = job_tabs[0] if job_tabs else 'active'
    
    now = timezone.now()
    
    if timeframe == 'weekly':
        start_date = now.date() - timedelta(days=now.weekday()) 
        label_prefix = "This Week's"
    elif timeframe == 'monthly':
        start_date = now.date().replace(day=1)
        label_prefix = "This Month's"
    elif timeframe == 'yearly':
        start_date = now.date().replace(month=1, day=1)
        label_prefix = "This Year's"
    else: 
        start_date = now.date()
        label_prefix = "Today's"

    date_filter = {'timestamp__date': start_date} if timeframe == 'daily' else {'timestamp__date__gte': start_date}
    session_date_filter = {'end_time__date': start_date} if timeframe == 'daily' else {'end_time__date__gte': start_date}

    # 1. Aggregate KPI Totals (Protected with Coalesce to eliminate 'None' bugs)
    total_extruded = ExtrusionLog.objects.filter(**date_filter).aggregate(
        total=Coalesce(Sum('roll_weight_kg'), Decimal('0.00'), output_field=DecimalField())
    )['total']
        
    total_cut = CuttingLog.objects.filter(**date_filter).aggregate(
        total=Coalesce(Sum('output_kg'), Decimal('0.00'), output_field=DecimalField())
    )['total']
        
    total_packed = PackingLog.objects.filter(**date_filter).annotate(
        weight=F('packing_size_kg') * F('quantity_packed')
    ).aggregate(
        total=Coalesce(Sum('weight'), Decimal('0.00'), output_field=DecimalField())
    )['total']

    # 2. Global Wastage Summaries for the Timeframe
    total_ext_waste = ExtrusionSession.objects.filter(**session_date_filter, status='COMPLETED').aggregate(
        total=Coalesce(Sum(F('total_wastage_kg') + F('final_wastage_kg')), Decimal('0.00'), output_field=DecimalField())
    )['total']
    
    total_cut_waste = CuttingSession.objects.filter(**session_date_filter, status='COMPLETED').aggregate(
        total=Coalesce(Sum('total_wastage_kg'), Decimal('0.00'), output_field=DecimalField())
    )['total']

    global_wastage = total_ext_waste + total_cut_waste

    # 3. Macro Breakdowns (Restored)
    extrusion_breakdown = ExtrusionLog.objects.filter(**date_filter).values(
        jo_num=F('session__job_order__jo_number'),
        customer=F('session__job_order__customer')
    ).annotate(total=Sum('roll_weight_kg')).order_by('-total')

    cutting_breakdown = CuttingLog.objects.filter(**date_filter).values(
        jo_num=F('session__job_order__jo_number'),
        customer=F('session__job_order__customer')
    ).annotate(total=Sum('output_kg')).order_by('-total')

    packing_breakdown = PackingLog.objects.filter(**date_filter).values(
        jo_num=F('job_order__jo_number'),
        customer=F('job_order__customer')
    ).annotate(total=Sum(F('packing_size_kg') * F('quantity_packed'))).order_by('-total')

    # 4. Live Operational Context (Restored)
    discrepancy_alerts = ExtrusionSession.objects.filter(
        unaccounted_variance_kg__gt=0
    ).select_related('job_order').order_by('-end_time')[:5]

    active_machines = ExtrusionSession.objects.filter(status='ACTIVE').select_related('job_order')
    active_cutting_machines = CuttingSession.objects.filter(status='ACTIVE').select_related('job_order')
    low_stock_materials = RawMaterial.objects.filter(current_stock_kg__lte=F('reorder_point_kg'))
    purchasing_shortfalls = MaterialAllocation.objects.filter(shortfall_kg__gt=0, job_order__is_completed=False)

    # 5. Optimised Pipeline Tiers (Using select_related to prevent N+1 Queries)
    active_jobs = JobOrder.objects.filter(
        Q(extrusion_sessions__status='ACTIVE') | Q(total_extruded_kg__gt=0),
        is_completed=False
    ).select_related('recipe').distinct().order_by('-id')[:10]

    queued_jobs = JobOrder.objects.filter(
        is_completed=False, 
        total_extruded_kg=0
    ).exclude(extrusion_sessions__status='ACTIVE').select_related('recipe').order_by('queue_position', 'target_delivery_date', 'id')[:10]
    
    completed_jobs = JobOrder.objects.filter(is_completed=True).select_related('recipe').order_by('-id')[:10]

    context = {
        'timeframe': timeframe,
        'expanded_sections': expanded_sections,
        'expanded_param': expanded_param,
        'job_tab': job_tab,
        'label_prefix': label_prefix,
        'total_extruded': total_extruded,
        'total_cut': total_cut,
        'total_packed': total_packed,
        'total_ext_waste': total_ext_waste,       # NEW
        'total_cut_waste': total_cut_waste,       # NEW
        'global_wastage': global_wastage,         # NEW
        'extrusion_breakdown': extrusion_breakdown,
        'cutting_breakdown': cutting_breakdown,
        'packing_breakdown': packing_breakdown,
        'active_machines': active_machines,
        'active_cutting_machines': active_cutting_machines,
        'low_stock_materials': low_stock_materials,
        'purchasing_shortfalls': purchasing_shortfalls,
        'discrepancy_alerts': discrepancy_alerts,
        'queued_jobs': queued_jobs,
        'active_jobs': active_jobs,
        'completed_jobs': completed_jobs,
    }
    
    if getattr(request, 'htmx', False) or request.headers.get('HX-Request') == 'true':
        return render(request, 'production/partials/tower_content.html', context)
    return render(request, 'production/control_tower.html', context)

def get_job_specs(request, jo_id):
    job_order = get_object_or_404(JobOrder, id=jo_id)

    if job_order.is_completed: 
        # Refactored to use the new Django template-based toast
        error_toast = render_toast("This Job Order is already closed or completed. You cannot view active specs for it.", "error")
        return HttpResponse(error_toast)
        
    dept = request.GET.get('dept')
    
    if not dept:
        usable_extruded = job_order.total_extruded_kg - job_order.total_cutting_wastage_kg
        
        if usable_extruded < job_order.order_quantity_kg:
            dept = 'extrusion'
        elif (job_order.total_cut_kg + job_order.total_cutting_wastage_kg) < job_order.total_extruded_kg:
            dept = 'cutting'
        else:
            dept = 'packing'
            
    return render(request, 'production/partials/job_spec_card.html', {'jo': job_order, 'dept': dept})
def custom_logout(request):
    """Safely ends the user session and returns to the gateway."""
    django_logout(request)
    # 'login' refers to the name='login' we defined in urls.py for the gateway
    return redirect('login')