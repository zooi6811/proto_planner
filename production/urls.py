from django.urls import path
from . import views

urlpatterns = [
    # Main Dashboards
    path('', views.operator_dashboard, name='dashboard'),
    path('control-tower/', views.control_tower, name='control_tower'),
    
    # HTMX Tab Fetching
    path('get-extrusion/', views.get_extrusion_form, name='get_extrusion'),
    path('get-cutting/', views.get_cutting_form, name='get_cutting'),
    path('get-packing/', views.get_packing_form, name='get_packing'),

    # Stateful Extrusion Sessions (NEW)
    path('load-machine-state/<str:machine_no>/', views.load_machine_state, name='load_machine_state'),
    path('start-session/', views.start_extrusion_session, name='start_session'),
    path('log-session-roll/', views.log_session_roll, name='log_session_roll'),
    path('stop-session/<int:session_id>/', views.stop_extrusion_session, name='stop_session'),

    # Form Submissions (Cutting & Packing)
    path('submit-cutting/', views.submit_cutting, name='submit_cutting'),
    path('submit-packing/', views.submit_packing, name='submit_packing'),

    # Utilities (Search & Digital Spec Sheets)
    path('get-job-specs/<int:jo_id>/', views.get_job_specs, name='get_job_specs'),
    path('search-jobs/', views.search_jobs, name='search_jobs'),
]