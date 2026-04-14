from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse
from django.db.models import Sum, F, Q
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils.html import escape
from decimal import Decimal, InvalidOperation
from .models import (JobOrder, ExtrusionLog, CuttingLog, PackingLog, RawMaterial, 
    MaterialUsageLog, MaterialAllocation, MaterialCategory, 
    ExtrusionSession, SessionMaterial)
from django.utils import timezone
import uuid
from datetime import timedelta
from django.utils.html import escape
from django.contrib.auth import authenticate, login as django_login
from django.contrib import messages
from .models import UserProfile
from django.contrib.auth.decorators import login_required
from django.contrib.auth import logout as django_logout

def gateway_login(request):
    # If already logged in, route them away from the login page
    if request.user.is_authenticated:
        if hasattr(request.user, 'profile') and request.user.profile.role == 'STAFF':
            return redirect('control_tower') # Ensure you have a url named 'control_tower'
        return redirect('dashboard') # Ensure you have a url named 'dashboard'

    if request.method == 'POST':
        login_type = request.POST.get('login_type')

        # 1. Handle Operator PIN Log-in
        if login_type == 'operator':
            pin = request.POST.get('pin_code')
            try:
                # Find the profile with this exact PIN
                profile = UserProfile.objects.get(pin_code=pin, role='OPERATOR')
                # Log the associated user in
                django_login(request, profile.user)
                return redirect('dashboard')
            except UserProfile.DoesNotExist:
                messages.error(request, "Invalid Operator PIN.")

        # 2. Handle Staff Username/Password Log-in
        elif login_type == 'staff':
            username = request.POST.get('username')
            password = request.POST.get('password')
            user = authenticate(request, username=username, password=password)
            
            if user is not None:
                django_login(request, user)
                # Check if they have a profile; default to operator terminal if not strictly STAFF
                if hasattr(user, 'profile') and user.profile.role == 'STAFF':
                    return redirect('control_tower')
                return redirect('dashboard')
            else:
                messages.error(request, "Invalid credentials or unauthorised access.")

    return render(request, 'production/login.html')

def get_toast_popup(message, alert_type="error", use_oob=True):
    """
    Generates a floating banner using pure CSS animations.
    When use_oob=False, it swaps perfectly into standard targets (like #feedback-container)
    which natively overwrites old toasts and permanently prevents stacking bugs.
    """
    safe_message = escape(str(message))
    
    # Configure colours and icons dynamically
    themes = {
        "error": {"bg": "#dc3545", "text": "white", "icon": "⚠️"},
        "success": {"bg": "#28a745", "text": "white", "icon": "✅"},
        "warning": {"bg": "#ffc107", "text": "#333", "icon": "⚠️"}
    }
    theme = themes.get(alert_type, themes["error"])

    oob_attr = 'hx-swap-oob="beforeend:body"' if use_oob else ''

    # Using pure CSS keyframes guarantees it fades away without relying on JS execution
    return f"""
    <div {oob_attr} style="position: fixed; top: 20px; right: 20px; z-index: 9999; padding: 15px 25px; background-color: {theme['bg']}; color: {theme['text']}; border-radius: 4px; font-size: 15px; font-weight: bold; box-shadow: 0 4px 10px rgba(0,0,0,0.3); animation: toastFadeOut 4s forwards; pointer-events: none;">
        {theme['icon']} {safe_message}
        <style>
            @keyframes toastFadeOut {{
                0% {{ opacity: 0; transform: translateY(-10px); }}
                10% {{ opacity: 1; transform: translateY(0); }}
                80% {{ opacity: 1; transform: translateY(0); }}
                100% {{ opacity: 0; transform: translateY(-10px); pointer-events: none; }}
            }}
        </style>
    </div>
    """

def parse_decimal(value):
    """Safely coerces form inputs into precise Decimals, bypassing TypeError crashes."""
    if value is None or str(value).strip() == '':
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None

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
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        material_id = request.POST.get('material_id')
        
        if not jo_id or not material_id:
            return HttpResponse(get_toast_popup("Please select both a Job Order and a Material.", "error"))

        amount_kg = parse_decimal(request.POST.get('amount_kg'))
        if amount_kg is None or amount_kg <= Decimal('0'):
            return HttpResponse(get_toast_popup("Invalid amount. Please enter positive numbers only.", "error"))

        material = get_object_or_404(RawMaterial, id=material_id)
        job_order = get_object_or_404(JobOrder, id=jo_id)

        if job_order.is_completed:
            return HttpResponse(get_toast_popup("This Job Order is already closed or completed. You cannot log new data against it.", "error"))

        if amount_kg > material.current_stock_kg:
            return HttpResponse(get_toast_popup(f"Insufficient stock. You requested {amount_kg}kg, but only {material.current_stock_kg}kg of {material.name} is available.", "error"))

        # Accurately trace the logged-in user rather than hardcoding "Extrusion Op"
        operator = request.user.username if request.user.is_authenticated else "Unknown Operator"

        usage_log = MaterialUsageLog.objects.create(
            job_order=job_order,
            material=material,
            amount_kg=amount_kg,
            operator_name=operator
        )
        
        allocation = MaterialAllocation.objects.filter(job_order=job_order, material=material).first()
        if allocation and allocation.is_overused:
            warning_msg = get_toast_popup(f"Material logged, but you have now exceeded the allocated formula limit for {material.name}!", "warning")
            return HttpResponse(warning_msg)
            
        success_msg = get_toast_popup(f"Successfully logged {amount_kg}kg of {material.name}.", "success")
        return HttpResponse(success_msg)

@login_required(login_url='login')
def operator_dashboard(request):
    job_orders = JobOrder.objects.all()
    return render(request, 'production/dashboard.html', {'job_orders': job_orders})

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
        job_orders = JobOrder.objects.filter(
            is_completed=False, 
            order_quantity_kg__gt=F('total_extruded_kg') - F('total_cutting_wastage_kg')
        ).order_by('-id')[:20]
        
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
        
        def reload_with_error(msg):
            # THE FIX: use_oob=False safely overwrites old errors in the container natively
            return HttpResponse(get_toast_popup(msg, "error", use_oob=False))

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

        success_toast = get_toast_popup(f"Session locked in on Machine {machine_no}. Extrusion active.", "success", use_oob=False)
        
        # THE FIX: Order HTMX to fetch the fresh 'Active Run' UI in the background and snap it into place
        soft_refresh = f"<script>htmx.ajax('GET', '/load-machine-state/{machine_no}/', {{target: '#machine-workspace'}});</script>"
        return HttpResponse(success_toast + soft_refresh)
    
def log_session_roll(request):
    """Operator logs a roll to their currently active session."""
    if request.method == "POST":
        session_id = request.POST.get('session_id')

        def reload_with_error(msg):
            return HttpResponse(get_toast_popup(msg, "error", use_oob=False))

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
            success_msg = get_toast_popup("Target Reached! Session Auto-Completed.", "success", use_oob=False)
        else:
            success_msg = get_toast_popup(f"Successfully logged {roll_weight}kg roll.", "success", use_oob=False)
            
        soft_refresh = f"""
        <script>
            var rw = document.querySelector('input[name="roll_weight"]'); if(rw) rw.value='';
            var wst = document.querySelector('input[name="wastage"]'); if(wst) wst.value='';
            htmx.ajax('GET', '/load-machine-state/{session.machine_no}/', {{target: '#machine-workspace'}});
        </script>
        """
        return HttpResponse(success_msg + soft_refresh)

def stop_extrusion_session(request, session_id):
    """Operator manually terminates the job early."""
    session = get_object_or_404(ExtrusionSession, id=session_id)
    session.stop_session()
    
    warning_toast = get_toast_popup(f"Session on Machine {session.machine_no} terminated early.", "warning", use_oob=False)
    
    soft_refresh = f"<script>htmx.ajax('GET', '/load-machine-state/{session.machine_no}/', {{target: '#machine-workspace'}});</script>"
    return HttpResponse(warning_toast + soft_refresh)

# -----------------------------------------------------------------------------
# CUTTING SUBMISSION
# -----------------------------------------------------------------------------
def submit_cutting(request):
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        
        def reload_with_error(error_text):
            return HttpResponse(get_toast_popup(error_text, "error", use_oob=False))

        if not jo_id:
            return reload_with_error("Please select an active job from the list first.")

        output_kg = parse_decimal(request.POST.get('output_kg'))
        wastage = parse_decimal(request.POST.get('wastage')) or Decimal('0')

        if output_kg is None or output_kg <= Decimal('0') or wastage < Decimal('0'):
            return reload_with_error("Invalid input. Weights must be proper, positive numbers.")
            
        if output_kg > Decimal('2000'): 
            return reload_with_error(f"{output_kg}kg exceeds the maximum allowed limit for a single entry. Check for typos.")

        job_order = get_object_or_404(JobOrder, id=jo_id)

        if job_order.is_completed:
            return reload_with_error("This Job Order is already closed or completed.")
        
        remaining_to_cut = job_order.total_extruded_kg - job_order.total_cut_kg
        
        if output_kg > (remaining_to_cut * Decimal('1.05')):
            return reload_with_error(f"Cannot log {output_kg}kg. Only {remaining_to_cut:.1f}kg remains from Extrusion.")

        operator = request.user.username if request.user.is_authenticated else "Unknown Operator"

        try:
            CuttingLog.objects.create(
                job_order=job_order,
                machine_no=request.POST.get('machine'),
                shift=request.POST.get('shift'),
                output_kg=output_kg,
                wastage_kg=wastage,
                operator_name=operator
            )
        except ValidationError as e:
            error_msg = " ".join(e.messages) if hasattr(e, 'messages') else str(e)
            return reload_with_error(error_msg)
            
        job_order.refresh_from_db()
        
        # 1. The Fading Success Toast
        success_toast = get_toast_popup(f"Successfully logged {output_kg}kg for {job_order.jo_number}.", "success", use_oob=False)
        
        # Determine if there is any material left to cut
        is_target_reached = str(job_order.total_cut_kg >= job_order.total_extruded_kg).lower()

        # 2. The Smart Soft Refresh Script
        soft_refresh_script = f"""
        <script>
            var out = document.querySelector('input[name="output_kg"]'); if(out) out.value='';
            var wst = document.querySelector('input[name="wastage"]'); if(wst) wst.value='';
            
            var searchInput = document.querySelector('input[name="q"]');
            if (searchInput) {{ 
                var url = searchInput.getAttribute('hx-get');
                var targetId = searchInput.getAttribute('hx-target').replace('#', '');
                var query = searchInput.value || '';
                
                // Listen for the exact moment the HTMX list update finishes
                var swapListener = function(e) {{
                    if (e.detail.target.id === targetId) {{
                        var radio = document.getElementById('jo_{job_order.id}');
                        var targetReached = {is_target_reached};
                        
                        // If the job is still active, re-select it and pull the fresh specs!
                        if (radio && !targetReached) {{
                            radio.checked = true;
                            fetch(`/get-job-specs/{job_order.id}/?dept=cutting`)
                                .then(response => response.text())
                                .then(html => {{
                                    var specContainer = document.getElementById('job-specs-container');
                                    if (specContainer) specContainer.innerHTML = html;
                                }});
                        }} else {{
                            // Only wipe the screen if the job is truly finished
                            var specContainer = document.getElementById('job-specs-container');
                            if (specContainer) {{
                                specContainer.innerHTML = '<div style="padding: 40px 20px; text-align: center; color: var(--text-muted); border: 2px dashed var(--border-soft); border-radius: var(--radius); margin-bottom: 20px;"><div style="font-size: 2.5rem; margin-bottom: 15px; opacity: 0.5;">✅</div><p style="font-weight: bold; text-transform: uppercase; margin: 0; font-size: 1.1rem;">Target Reached</p><p style="font-size: 0.9rem; color: var(--border-hard); margin-top: 8px;">Select a new job to continue.</p></div>';
                            }}
                            var progressBar = document.getElementById('active-progress-bar');
                            if (progressBar) progressBar.innerHTML = '';
                        }}
                        document.body.removeEventListener('htmx:afterSwap', swapListener);
                    }}
                }};
                document.body.addEventListener('htmx:afterSwap', swapListener);
                htmx.ajax('GET', url + '&q=' + encodeURIComponent(query), {{target: '#' + targetId}}); 
            }}
        </script>
        """
        return HttpResponse(success_toast + soft_refresh_script)
    
# -----------------------------------------------------------------------------
# PACKING SUBMISSION
# -----------------------------------------------------------------------------
def submit_packing(request):
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        
        def reload_with_error(error_text):
            return HttpResponse(get_toast_popup(error_text, "error", use_oob=False))

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
            
        success_toast = get_toast_popup(success_msg, "success", use_oob=False)
        
        # Determine if the entire job order is fulfilled
        is_target_reached = str(job_order.is_completed).lower()
        
        # 2. The Smart Soft Refresh Script
        soft_refresh_script = f"""
        <script>
            var ps = document.querySelector('input[name="packing_size"]'); if(ps) ps.value='';
            var qty = document.querySelector('input[name="quantity"]'); if(qty) qty.value='';
            
            var searchInput = document.querySelector('input[name="q"]');
            if (searchInput) {{ 
                var url = searchInput.getAttribute('hx-get');
                var targetId = searchInput.getAttribute('hx-target').replace('#', '');
                var query = searchInput.value || '';
                
                var swapListener = function(e) {{
                    if (e.detail.target.id === targetId) {{
                        var radio = document.getElementById('jo_{job_order.id}');
                        var targetReached = {is_target_reached};
                        
                        if (radio && !targetReached) {{
                            radio.checked = true;
                            fetch(`/get-job-specs/{job_order.id}/?dept=packing`)
                                .then(response => response.text())
                                .then(html => {{
                                    var specContainer = document.getElementById('job-specs-container');
                                    if (specContainer) specContainer.innerHTML = html;
                                }});
                        }} else {{
                            var specContainer = document.getElementById('job-specs-container');
                            if (specContainer) {{
                                specContainer.innerHTML = '<div style="padding: 40px 20px; text-align: center; color: var(--text-muted); border: 2px dashed var(--border-soft); border-radius: var(--radius); margin-bottom: 20px;"><div style="font-size: 2.5rem; margin-bottom: 15px; opacity: 0.5;">✅</div><p style="font-weight: bold; text-transform: uppercase; margin: 0; font-size: 1.1rem;">Order Fulfilled</p><p style="font-size: 0.9rem; color: var(--border-hard); margin-top: 8px;">Select a new job to continue.</p></div>';
                            }}
                            var progressBar = document.getElementById('active-progress-bar');
                            if (progressBar) progressBar.innerHTML = '';
                        }}
                        document.body.removeEventListener('htmx:afterSwap', swapListener);
                    }}
                }};
                document.body.addEventListener('htmx:afterSwap', swapListener);
                htmx.ajax('GET', url + '&q=' + encodeURIComponent(query), {{target: '#' + targetId}}); 
            }}
        </script>
        """
        return HttpResponse(success_toast + soft_refresh_script)
    
# -----------------------------------------------------------------------------
# HTMX FORM FETCHING & SEARCHING
# -----------------------------------------------------------------------------
@login_required(login_url='login')
def get_extrusion_form(request):
    job_orders = JobOrder.objects.filter(
        is_completed=False, 
        order_quantity_kg__gt=F('total_extruded_kg') - F('total_cutting_wastage_kg')
    ).order_by('-id')[:20]
    
    # Fetch a flat list of machines that currently have an active session
    active_machines = ExtrusionSession.objects.filter(status='ACTIVE').values_list('machine_no', flat=True)
    
    return render(request, 'production/partials/extrusion_form.html', {
        'job_orders': job_orders,
        'active_machines': list(active_machines) # Convert QuerySet to list for easy template checking
    })

@login_required(login_url='login')
def get_cutting_form(request):
    job_orders = JobOrder.objects.filter(
        is_completed=False,
        total_extruded_kg__gt=F('total_cut_kg') + F('total_cutting_wastage_kg')
    ).order_by('-id')[:20]
    return render(request, 'production/partials/cutting_form.html', {'job_orders': job_orders, 'dept': 'cutting'})

@login_required(login_url='login')
def get_packing_form(request):
    job_orders = JobOrder.objects.filter(
        is_completed=False,
        total_cut_kg__gt=F('total_packed_kg')
    ).order_by('-id')[:20]
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
        
    return render(request, 'production/partials/job_radio_list.html', {'job_orders': jobs[:20], 'dept': dept})

# -----------------------------------------------------------------------------
# DASHBOARD & TOWER LOGIC
# -----------------------------------------------------------------------------
@login_required(login_url='login')
def control_tower(request):
    if hasattr(request.user, 'profile') and request.user.profile.role == 'OPERATOR':
        return redirect('dashboard')
    
    # Core States Handling
    timeframes = request.GET.getlist('timeframe')
    timeframe = timeframes[0] if timeframes else 'daily'
    
    expanded_param = request.GET.get('expanded', '')
    expanded_sections = expanded_param.split(',') if expanded_param else []
    
    job_tabs = request.GET.getlist('job_tab')
    job_tab = job_tabs[0] if job_tabs else 'active'
    
    now = timezone.now()
    
    # Timeframe Resolutions
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

    if timeframe == 'daily':
        date_filter = {'timestamp__date': start_date}
    else:
        date_filter = {'timestamp__date__gte': start_date}

    # Aggregate KPI Totals
    total_extruded = ExtrusionLog.objects.filter(**date_filter).aggregate(
        total=Sum('roll_weight_kg'))['total'] or Decimal('0')
        
    total_cut = CuttingLog.objects.filter(**date_filter).aggregate(
        total=Sum('output_kg'))['total'] or Decimal('0')
        
    total_packed = PackingLog.objects.filter(**date_filter).annotate(
        weight=F('packing_size_kg') * F('quantity_packed')
    ).aggregate(total=Sum('weight'))['total'] or Decimal('0')

    # Macro Breakdowns
    extrusion_breakdown = ExtrusionLog.objects.filter(**date_filter).values(
        jo_num=F('session__job_order__jo_number'),
        customer=F('session__job_order__customer')
    ).annotate(total=Sum('roll_weight_kg')).order_by('-total')

    cutting_breakdown = CuttingLog.objects.filter(**date_filter).values(
        jo_num=F('job_order__jo_number'),
        customer=F('job_order__customer')
    ).annotate(total=Sum('output_kg')).order_by('-total')

    packing_breakdown = PackingLog.objects.filter(**date_filter).values(
        jo_num=F('job_order__jo_number'),
        customer=F('job_order__customer')
    ).annotate(total=Sum(F('packing_size_kg') * F('quantity_packed'))).order_by('-total')

    # Live Operational Context
    active_machines = ExtrusionSession.objects.filter(status='ACTIVE').select_related('job_order')
    low_stock_materials = RawMaterial.objects.filter(current_stock_kg__lte=F('reorder_point_kg'))
    purchasing_shortfalls = MaterialAllocation.objects.filter(shortfall_kg__gt=0, job_order__is_completed=False)
    
    # Factory Pipeline Tiers
    active_jobs = JobOrder.objects.filter(
        Q(extrusion_sessions__status='ACTIVE') | Q(total_extruded_kg__gt=0),
        is_completed=False
    ).distinct().order_by('-id')[:10]

    queued_jobs = JobOrder.objects.filter(
        is_completed=False, 
        total_extruded_kg=0
    ).exclude(extrusion_sessions__status='ACTIVE').order_by('id')[:10]
    
    completed_jobs = JobOrder.objects.filter(is_completed=True).order_by('-id')[:10]

    context = {
        'timeframe': timeframe,
        'expanded_sections': expanded_sections,
        'expanded_param': expanded_param,
        'job_tab': job_tab,
        'label_prefix': label_prefix,
        'total_extruded': total_extruded,
        'total_cut': total_cut,
        'total_packed': total_packed,
        'extrusion_breakdown': extrusion_breakdown,
        'cutting_breakdown': cutting_breakdown,
        'packing_breakdown': packing_breakdown,
        'active_machines': active_machines,
        'low_stock_materials': low_stock_materials,
        'purchasing_shortfalls': purchasing_shortfalls,
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
        return HttpResponse(get_toast_popup("This Job Order is already closed or completed. You cannot view active specs for it.", "error"))
        
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