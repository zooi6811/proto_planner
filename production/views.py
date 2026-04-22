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
    ExtrusionSession, SessionMaterial, AuditLog)
from django.utils import timezone
import uuid
from datetime import timedelta
from django.contrib.auth import authenticate, login as django_login
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import logout as django_logout
from django.contrib.auth.models import User
from django.views.decorators.cache import never_cache
import json
from django.template.loader import render_to_string
from functools import wraps

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
    trigger_data = {
        "softRefresh": {
            "url": url,
            "target": target
        }
    }
    response['HX-Trigger'] = json.dumps(trigger_data)
    return response

def trigger_packing_refresh(response, job_id, is_completed):
    trigger_data = {
        "packingRefresh": {
            "jobId": job_id,
            "isCompleted": is_completed
        }
    }
    response['HX-Trigger'] = json.dumps(trigger_data)
    return response

def htmx_toast_response(message, alert_type="success", refresh_url=None, refresh_target=None):
    response = HttpResponse(render_toast(message, alert_type, use_oob=False))
    if refresh_url and refresh_target:
        return trigger_refresh(response, refresh_url, refresh_target)
    return response

def require_logging_permission(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not has_logging_permission(request.user):
            return htmx_toast_response("Unauthorised: Only Operators and Admins can perform this action.", "error")
        return view_func(request, *args, **kwargs)
    return _wrapped_view

def parse_decimal(value):
    if value is None or str(value).strip() == '':
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    
def has_logging_permission(user):
    if not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    if hasattr(user, 'profile') and user.profile.role == 'OPERATOR':
        return True
    return False

def add_material_row(request):
    try:
        row_id = str(uuid.uuid4())[:8]
        categories = MaterialCategory.objects.all().order_by('name')
        return render(request, 'production/partials/material_row.html', {'row_id': row_id, 'categories': categories})
    except Exception as e:
        return HttpResponse(f'<div style="color:var(--status-red); padding:10px; border:1px solid red;">Backend Error: {str(e)}</div>')
    
def get_materials_by_category(request):
    try:
        cat_id = None
        for key, value in request.GET.items():
            if key.startswith('category_'):
                cat_id = value
                break
                
        if not cat_id:
            cat_id = request.GET.get('category_id')

        if not cat_id or str(cat_id).strip() == '':
            return HttpResponse('<option value="" selected disabled>-- Awaiting Category --</option>')

        clean_id = int(str(cat_id).strip())
        materials = RawMaterial.objects.filter(category_id=clean_id).order_by('name')
        return render(request, 'production/partials/material_options.html', {'materials': materials})

    except (ValueError, TypeError, Exception):
        return HttpResponse('<option value="" selected disabled>-- Selection Error --</option>')

def submit_material_usage(request):
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
        operator = request.user.username if request.user.is_authenticated else "Unknown Operator"

        try:
            log, is_overused = MaterialUsageLog.record_usage(
                job_order=job_order,
                material=material,
                amount_kg=amount_kg,
                operator_name=operator
            )

            AuditLog.objects.create(
                operator_name=operator,
                action_type='MATERIAL_USED',
                content_object=job_order,
                details={
                    'material_name': material.name,
                    'amount_kg': str(amount_kg),
                    'is_overused_flag': is_overused,
                    'remaining_warehouse_stock': str(material.current_stock_kg)
                }
            )

            if is_overused:
                return htmx_toast_response(f"Material logged, but you have now exceeded the allocated formula limit for {material.name}!", "warning")
                
            return htmx_toast_response(f"Successfully logged {amount_kg}kg of {material.name}.", "success")
        
        except ValidationError as e:
            error_message = e.messages[0] if hasattr(e, 'messages') else str(e)
            return HttpResponse(render_toast(error_message, "error"))
        
@login_required(login_url='login')
def operator_dashboard(request):
    job_orders = JobOrder.objects.filter(
        is_completed=False, 
        order_quantity_kg__gt=F('total_extruded_kg') - F('total_cutting_wastage_kg')
    ).order_by('queue_position', 'target_delivery_date', 'id')[:20]
    
    active_machines = ExtrusionSession.objects.filter(status='ACTIVE').values_list('machine_no', flat=True)
    
    return render(request, 'production/dashboard.html', {
        'job_orders': job_orders,
        'active_machines': list(active_machines)
    })

def load_machine_state(request, machine_no=None):
    if not machine_no:
        machine_no = request.GET.get('machine_no')
        
    if not machine_no:
        return HttpResponse("<p style='color: var(--text-muted); font-weight: bold; text-transform: uppercase;'>Awaiting Machine Selection...</p>")

    active_session = ExtrusionSession.objects.filter(machine_no=machine_no, status='ACTIVE').first()
    
    if active_session:
        queued_jobs = JobOrder.objects.filter(is_completed=False, order_quantity_kg__gt=0).exclude(id=active_session.job_order.id).order_by('queue_position')[:15]
        
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
    
@require_logging_permission
def start_extrusion_session(request):
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        machine_no = request.POST.get('machine_no')
        shift = request.POST.get('shift')

        if not jo_id:
            return htmx_toast_response("Please select a Job Order from the list.", "error")

        target_amount = parse_decimal(request.POST.get('target_amount'))
        if target_amount is None or target_amount <= Decimal('0'):
            return htmx_toast_response("Invalid target amount. Please check your numbers.", "error")
            
        material_ids = request.POST.getlist('material_ids')
        reserved_amounts_raw = request.POST.getlist('reserved_amounts')
        
        material_reservations = []
        for mat_id, amt_raw in zip(material_ids, reserved_amounts_raw):
            amt = parse_decimal(amt_raw) if amt_raw.strip() else Decimal('0')
            if amt is None or amt < Decimal('0'):
                return htmx_toast_response("Invalid material reservation amounts. Ensure they are valid numbers.", "error")
            material_reservations.append((mat_id, amt))

        total_reserved = sum(amt for _, amt in material_reservations)

        if total_reserved < target_amount:
            return htmx_toast_response(f"Total reserved material ({total_reserved}kg) cannot be less than target ({target_amount}kg).", "error")
        
        job_order = get_object_or_404(JobOrder, id=jo_id)
        if job_order.is_completed: 
            return htmx_toast_response("This Job Order is already closed or completed.", "error")
        
        operator = request.user.username if request.user.is_authenticated else "Unknown Operator"

        try:
            ExtrusionSession.start_session(
                job_order=job_order, machine_no=machine_no, shift=shift, 
                target_amount=target_amount, operator=operator, 
                material_reservations=material_reservations
            )
        except ValidationError as e:
            error_msg = " ".join(e.messages) if hasattr(e, 'messages') else str(e)
            return htmx_toast_response(error_msg, "error")

        return htmx_toast_response(
            f"Session locked in on Machine {machine_no}. Extrusion active.", 
            "success", 
            f"/load-machine-state/{machine_no}/", 
            "#machine-workspace"
        )

@require_logging_permission
def log_session_roll(request):
    if request.method == "POST":
        session_id = request.POST.get('session_id')

        if not session_id:
            return htmx_toast_response("No active session found.", "error")
            
        session = get_object_or_404(ExtrusionSession, id=session_id)
        submitted_version = request.POST.get('session_version')
        operator = request.user.username if request.user.is_authenticated else "Unknown Operator"
        
        roll_weight = parse_decimal(request.POST.get('roll_weight'))
        wastage = parse_decimal(request.POST.get('wastage')) or Decimal('0')
        
        if roll_weight is None or roll_weight <= Decimal('0'):
            return htmx_toast_response("Roll weight must be strictly greater than zero.", "error")
        
        if roll_weight > Decimal('500'): 
            return htmx_toast_response(f"{roll_weight}kg exceeds maximum physical roll capacity. Check for typos.", "error")

        try:
            ExtrusionLog.record_log(
                session=session, roll_weight=roll_weight, wastage=wastage, 
                submitted_version=submitted_version, operator=operator
            )
        except ValidationError as e:
            error_msg = " ".join(e.messages) if hasattr(e, 'messages') else str(e)
            return htmx_toast_response(error_msg, "error")
        
        session.refresh_from_db() 
        
        if session.status == 'COMPLETED':
            msg, tag = ("Target Reached! Session Auto-Completed.", "success") if session.total_output_kg >= session.target_amount_kg else ("Material Depleted! Session Auto-Completed.", "warning")
        else:
            msg, tag = (f"Successfully logged {roll_weight}kg roll.", "success")
            
        response = htmx_toast_response(msg, tag, f"/load-machine-state/{session.machine_no}/", "#machine-workspace")
        response['HX-Retarget'] = 'body'
        response['HX-Reswap'] = 'beforeend'
        
        return response
    
def handover_extrusion_shift(request, session_id):
    if request.method == 'POST':
        session = get_object_or_404(ExtrusionSession, id=session_id, status='ACTIVE')
        new_operator = request.POST.get('operator_name')
        new_shift = request.POST.get('shift')
        
        session.handover_shift(new_operator, new_shift)
        
        success_toast = render_toast(f"Shift Handed Over to {new_operator}.", "success", use_oob=False)
        response = HttpResponse(success_toast)
        return trigger_refresh(response, f"/load-machine-state/{session.machine_no}/", "#machine-workspace")
    
def rollover_extrusion_session(request, session_id):
    if request.method == 'POST':
        old_session = get_object_or_404(ExtrusionSession, id=session_id, status='ACTIVE')
        next_job_id = request.POST.get('next_job_order_id')
        next_job = get_object_or_404(JobOrder, id=next_job_id)
        
        new_session = old_session.rollover_to_job(next_job)
        
        success_toast = render_toast("Material Rolled Over to New Job.", "success", use_oob=False)
        response = HttpResponse(success_toast)
        return trigger_refresh(response, f"/load-machine-state/{new_session.machine_no}/", "#machine-workspace")

def purge_and_close_session(request, session_id):
    if request.method == 'POST':
        session = get_object_or_404(ExtrusionSession, id=session_id)
        
        returned_kg = parse_decimal(request.POST.get('returned_material_kg', '0')) or Decimal('0')
        final_waste = parse_decimal(request.POST.get('final_wastage_kg', '0')) or Decimal('0')
        force_discrepancy = request.POST.get('submit_with_discrepancy') == 'true'
        
        try:
            session.purge_and_close(
                returned_kg=returned_kg,
                final_waste=final_waste,
                force_discrepancy=force_discrepancy
            )
        except ValidationError as e:
            error_msg = e.messages[0] if hasattr(e, 'messages') else str(e)
            return HttpResponse(render_toast(error_msg, "error", use_oob=False))
        
        msg = "Session Closed with Discrepancy Flag." if force_discrepancy else "Session Cleanly Closed."
        toast_type = "warning" if force_discrepancy else "success"
        
        success_toast = render_toast(msg, toast_type, use_oob=False)
        response = HttpResponse(success_toast)
        return trigger_refresh(response, f"/load-machine-state/{session.machine_no}/", "#machine-workspace")

@login_required(login_url='login')
def complete_extrusion_session(request, session_id):
    if request.method == 'POST':
        session = get_object_or_404(ExtrusionSession, id=session_id, status='ACTIVE')
        machine_no = session.machine_no
        
        session.status = 'COMPLETED'
        session.save(update_fields=['status'])
        
        success_toast = render_toast(f"Extrusion on Machine {machine_no} completed successfully.", "success", use_oob=False)
        response = HttpResponse(success_toast)
        return trigger_refresh(response, f"/load-machine-state/{machine_no}/", "#machine-workspace")

@require_logging_permission
def stop_extrusion_session(request, session_id):
    session = get_object_or_404(ExtrusionSession, id=session_id)
    operator = request.user.username if request.user.is_authenticated else "Unknown Operator"
    
    machine_no = session.machine_no
    shift = session.shift
    
    session.terminate_early(operator)
    
    warning_toast = htmx_toast_response(f"Session on Machine {machine_no} terminated early.", "warning", use_oob=False)
    
    ajax_url = f"/load-machine-state/{machine_no}/?prefill_machine={machine_no}&prefill_shift={shift}"
    response = HttpResponse(warning_toast)
    return trigger_refresh(response, ajax_url, "#machine-workspace")

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
        operator = request.user.username if request.user.is_authenticated else "Unknown Operator"

        try:
            CuttingSession.start_session(
                job_order=job_order, machine_no=machine_no, shift=shift, 
                input_roll=input_roll, operator=operator
            )
        except ValidationError as e:
            return reload_with_error(str(e.messages[0] if hasattr(e, 'messages') else str(e)))

        success_toast = render_toast(f"Session locked on Cut Machine {machine_no}.", "success", use_oob=False)
        response = HttpResponse(success_toast)
        return trigger_refresh(response, f"/load-cutting-state/{machine_no}/", "#cutting-workspace")

def log_cut_roll(request):
    if not has_logging_permission(request.user):
        return HttpResponse(render_toast("Unauthorised: Only Operators can log output.", "error", use_oob=False))
        
    if request.method == "POST":
        session_id = request.POST.get('session_id')

        def reload_with_error(msg):
            return HttpResponse(render_toast(msg, "error", use_oob=False))

        if not session_id:
            return reload_with_error("No active session found.")
            
        session = get_object_or_404(CuttingSession, id=session_id)
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
    if not has_logging_permission(request.user):
        return HttpResponse(render_toast("Unauthorised action.", "error", use_oob=False))
        
    session = get_object_or_404(CuttingSession, id=session_id)
    session.stop_session(calculate_wastage=False)
    
    warning_toast = render_toast(f"Cutting Session on Machine {session.machine_no} ended early. Wastage deferred.", "warning", use_oob=False)
    response = HttpResponse(warning_toast)
    return trigger_refresh(response, f"/load-cutting-state/{session.machine_no}/", "#cutting-workspace")

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

        job_order = get_object_or_404(JobOrder, id=jo_id)
        operator = request.user.username if request.user.is_authenticated else "Unknown Operator"

        try:
            PackingLog.record_packing(
                job_order=job_order,
                packing_size=packing_size,
                quantity=quantity,
                operator=operator
            )
        except ValidationError as e:
            error_msg = " ".join(e.messages) if hasattr(e, 'messages') else str(e)
            return reload_with_error(error_msg)

        job_order.refresh_from_db()

        if job_order.total_packed_kg >= job_order.order_quantity_kg:
            success_msg = f"Target Reached! Job {job_order.jo_number} is now fully packed and closed."
        else:
            total_weight_submitting = packing_size * Decimal(str(quantity))
            success_msg = f"Successfully packed {total_weight_submitting}kg for {job_order.jo_number}."
            
        success_toast = render_toast(success_msg, "success", use_oob=False)
        is_target_reached = bool(job_order.is_completed)
        
        response = HttpResponse(success_toast)
        return trigger_packing_refresh(response, job_order.id, is_target_reached)
    
@login_required(login_url='login')
def get_extrusion_form(request):
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
    prefill_machine = request.GET.get('prefill_machine', '')
    prefill_shift = request.GET.get('prefill_shift', '')
    
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
        
    jobs = jobs.order_by('queue_position', 'target_delivery_date', 'id')
        
    return render(request, 'production/partials/job_radio_list.html', {'job_orders': jobs[:20], 'dept': dept})

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

    total_ext_waste = ExtrusionSession.objects.filter(**session_date_filter, status='COMPLETED').aggregate(
        total=Coalesce(Sum(F('total_wastage_kg') + F('final_wastage_kg')), Decimal('0.00'), output_field=DecimalField())
    )['total']
    
    total_cut_waste = CuttingSession.objects.filter(**session_date_filter, status='COMPLETED').aggregate(
        total=Coalesce(Sum('total_wastage_kg'), Decimal('0.00'), output_field=DecimalField())
    )['total']

    # We use the already-optimised manager to get active jobs
    active_jobs = JobOrder.objects.active_jobs(limit=10)
    
    # Extract unique recipes currently in production purely in Python to avoid extra DB queries
    active_recipes = {job.recipe for job in active_jobs if job.recipe}

    context = {
        'timeframe': timeframe,
        'expanded_sections': expanded_sections,
        'expanded_param': expanded_param,
        'job_tab': job_tab,
        'label_prefix': label_prefix,
        
        'total_extruded': ExtrusionLog.get_total_output(date_filter),
        'total_cut': CuttingLog.get_total_output(date_filter),
        'total_packed': PackingLog.get_total_output(date_filter),
        
        'total_ext_waste': total_ext_waste,       
        'total_cut_waste': total_cut_waste,       
        'global_wastage': total_ext_waste + total_cut_waste,         
        
        'extrusion_breakdown': ExtrusionLog.get_macro_breakdown(date_filter),
        'cutting_breakdown': CuttingLog.get_macro_breakdown(date_filter),
        'packing_breakdown': PackingLog.get_macro_breakdown(date_filter),
        
        'active_machines': ExtrusionSession.objects.filter(status='ACTIVE').select_related('job_order'),
        'active_cutting_machines': CuttingSession.objects.filter(status='ACTIVE').select_related('job_order'),
        'low_stock_materials': RawMaterial.objects.filter(current_stock_kg__lte=F('reorder_point_kg')),
        'purchasing_shortfalls': MaterialAllocation.objects.filter(shortfall_kg__gt=0, job_order__is_completed=False),
        'discrepancy_alerts': ExtrusionSession.objects.filter(unaccounted_variance_kg__gt=0).select_related('job_order').order_by('-end_time')[:5],
        
        'queued_jobs': JobOrder.objects.queued_jobs(limit=10),
        'active_jobs': JobOrder.objects.active_jobs(limit=10),
        'completed_jobs': JobOrder.objects.completed_jobs(limit=10),

        'live_yield_tracker': active_recipes,

        'recent_activity_feed': AuditLog.objects.all()[:15],
    }
    
    if getattr(request, 'htmx', False) or request.headers.get('HX-Request') == 'true':
        return render(request, 'production/partials/tower_content.html', context)
    return render(request, 'production/control_tower.html', context)

def get_job_specs(request, jo_id):
    job_order = get_object_or_404(JobOrder, id=jo_id)

    if job_order.is_completed: 
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
    django_logout(request)
    return redirect('login')

@require_logging_permission
def force_close_job(request, jo_id):
    if request.method == "POST":
        job = get_object_or_404(JobOrder, id=jo_id)
        
        # Security: Strictly restrict this to Staff/Supervisors
        if hasattr(request.user, 'profile') and request.user.profile.role != 'STAFF':
            error_toast = render_toast("Unauthorised: Only Management or Staff can authorise a forced closure.", "error", use_oob=False)
            return HttpResponse(error_toast)
            
        operator = request.user.username if request.user.is_authenticated else "System"
        
        # Attempt the closure with force_close=True
        success, message = job.complete_job(operator_name=operator, force_close=True)
        
        if success:
            # Tell the frontend to refresh the active jobs tab
            success_toast = render_toast(message, "success", use_oob=False)
            response = HttpResponse(success_toast)
            return trigger_refresh(response, f"/control-tower/", "body")
        else:
            error_toast = render_toast(message, "error", use_oob=False)
            return HttpResponse(error_toast)