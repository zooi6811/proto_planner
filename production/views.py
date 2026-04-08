from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse
from .models import JobOrder, ExtrusionLog, CuttingLog, PackingLog, RawMaterial
from django.db.models import F
from django.db.models import Q

def operator_dashboard(request):
    job_orders = JobOrder.objects.all()
    return render(request, 'production/dashboard.html', {'job_orders': job_orders})

# -----------------------------------------------------------------------------
# EXTRUSION SUBMISSION
# -----------------------------------------------------------------------------
def submit_extrusion(request):
    if request.method == "POST":
        jo_id = request.POST.get('job_order')
        
        try:
            roll_weight = float(request.POST.get('roll_weight'))
            wastage = float(request.POST.get('wastage') or 0)
        except ValueError:
            return render(request, 'production/partials/error_message.html', 
                          {'error': "Invalid input. Please enter numbers only."})

        # Logic & Bounds Checks
        if roll_weight <= 0 or wastage < 0:
            return render(request, 'production/partials/error_message.html', 
                          {'error': "Weights cannot be zero or negative."})
        if wastage > (roll_weight * 0.5):
            return render(request, 'production/partials/error_message.html', 
                          {'error': "Wastage is suspiciously high (> 50%). Please verify."})
        if roll_weight > 300: 
            return render(request, 'production/partials/error_message.html', 
                          {'error': f"A {roll_weight}kg roll exceeds the maximum machine limit."})

        job_order = get_object_or_404(JobOrder, id=jo_id)
        
        ExtrusionLog.objects.create(
            job_order=job_order,
            machine_no=request.POST.get('machine'),
            shift=request.POST.get('shift'),
            roll_weight_kg=roll_weight,
            wastage_kg=wastage,
            operator_name="Extrusion Op"
        )
        job_order.refresh_from_db()
        return render(request, 'production/partials/progress_bar.html', {'jo': job_order})

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
            return render(request, 'production/partials/error_message.html', 
                          {'error': "Invalid input. Please enter numbers only."})

        # Logic & Bounds Checks
        if output_kg <= 0 or wastage < 0:
            return render(request, 'production/partials/error_message.html', 
                          {'error': "Weights cannot be zero or negative."})
        if output_kg > 1000: # Adjust based on maximum reasonable shift output
            return render(request, 'production/partials/error_message.html', 
                          {'error': f"An output of {output_kg}kg seems too high for a single entry."})

        job_order = get_object_or_404(JobOrder, id=jo_id)
        
        # Ensure Cutting doesn't exceed Extrusion
        if (job_order.total_cut_kg + output_kg) > (job_order.total_extruded_kg * 1.05): # 5% buffer leeway
            return render(request, 'production/partials/error_message.html', 
                          {'error': "Cannot cut more material than the Extrusion department has produced!"})

        CuttingLog.objects.create(
            job_order=job_order,
            machine_no=request.POST.get('machine'),
            shift=request.POST.get('shift'),
            output_kg=output_kg,
            wastage_kg=wastage,
            operator_name="Cutting Op"
        )
        job_order.refresh_from_db()
        return render(request, 'production/partials/progress_bar.html', {'jo': job_order})

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
            return render(request, 'production/partials/error_message.html', 
                          {'error': "Invalid input. Ensure quantity is a whole number."})

        # Logic & Bounds Checks
        if packing_size <= 0 or quantity <= 0:
            return render(request, 'production/partials/error_message.html', 
                          {'error': "Values must be greater than zero."})

        total_weight_submitting = packing_size * quantity
        job_order = get_object_or_404(JobOrder, id=jo_id)

        PackingLog.objects.create(
            job_order=job_order,
            packing_size_kg=packing_size,
            quantity_packed=quantity,
            operator_name="Packing Op"
        )
        job_order.refresh_from_db()
        return render(request, 'production/partials/progress_bar.html', {'jo': job_order})
    
# -----------------------------------------------------------------------------
# HTMX FORM FETCHING & SEARCHING
# -----------------------------------------------------------------------------
def get_extrusion_form(request):
    # Queue: Only show jobs that have not met their estimated material target, limit to 20
    job_orders = JobOrder.objects.filter(total_extruded_kg__lt=F('order_quantity_kg')).order_by('-id')[:20]
    return render(request, 'production/partials/extrusion_form.html', {'job_orders': job_orders})

def get_cutting_form(request):
    # Queue: Only show jobs that have been extruded but not fully cut
    job_orders = JobOrder.objects.filter(total_extruded_kg__gt=0).order_by('-id')[:20]
    return render(request, 'production/partials/cutting_form.html', {'job_orders': job_orders})

def get_packing_form(request):
    # Queue: Only show jobs that have been cut (or extruded) and need packing
    job_orders = JobOrder.objects.filter(total_extruded_kg__gt=0).order_by('-id')[:20]
    return render(request, 'production/partials/packing_form.html', {'job_orders': job_orders})

def search_jobs(request):
    """
    Live HTMX endpoint that filters jobs as the operator types.
    """
    query = request.GET.get('q', '')
    
    # Start with all active jobs
    jobs = JobOrder.objects.filter(order_quantity_kg__gt=0)
    
    if query:
        # Search BOTH the JO Number and the Customer Name, ignoring case
        jobs = jobs.filter(
            Q(jo_number__icontains=query) | Q(customer__icontains=query)
        )
        
    # Return just the HTML snippet of the filtered radio buttons, limited to 20 results
    return render(request, 'production/partials/job_radio_list.html', {'job_orders': jobs[:20]})

# -----------------------------------------------------------------------------
# CENTRAL ADMIN DASHBOARD (CONTROL TOWER)
# -----------------------------------------------------------------------------
def control_tower(request):
    # 1. Fetch Critical Alerts (Stock below reorder point)
    low_stock_materials = RawMaterial.objects.filter(current_stock_kg__lte=F('reorder_point_kg'))
    
    # 2. Fetch Active Job Orders (Jobs that are not 100% complete)
    # Note: In a full system, you might add a 'status' field to JobOrder. 
    # Here we filter by jobs that have an order quantity greater than 0.
    active_jobs = JobOrder.objects.filter(order_quantity_kg__gt=0).order_by('-id')[:10]
    
    # 3. Determine Machine Status based on recent logs
    # For this prototype, we will fetch the 5 most recent logs across all departments
    recent_extrusion = ExtrusionLog.objects.select_related('job_order').order_by('-timestamp')[:5]
    recent_cutting = CuttingLog.objects.select_related('job_order').order_by('-timestamp')[:5]
    
    context = {
        'low_stock_materials': low_stock_materials,
        'active_jobs': active_jobs,
        'recent_extrusion': recent_extrusion,
        'recent_cutting': recent_cutting,
    }
    
    # If it's an HTMX request (auto-polling), only return the inner content
    if request.htmx:
        return render(request, 'production/partials/tower_content.html', context)
        
    # If it's a full page load, return the full shell
    return render(request, 'production/control_tower.html', context)

def get_job_specs(request, jo_id):
    job_order = get_object_or_404(JobOrder, id=jo_id)
    return render(request, 'production/partials/job_spec_card.html', {'jo': job_order})