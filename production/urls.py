from django.urls import path
from . import views
from django.contrib.auth.views import LogoutView

urlpatterns = [
    # Gateway & Authentication
    path('login/', views.gateway_login, name='login'),
    path('logout/', LogoutView.as_view(next_page='login'), name='logout'),
    path('register/', views.register_user, name='register_user'),

    # Main Dashboards
    path('', views.operator_dashboard, name='dashboard'),
    path('control-tower/', views.control_tower, name='control_tower'),
    
    # HTMX Tab Fetching
    path('get-extrusion/', views.get_extrusion_form, name='get_extrusion'),
    path('get-cutting/', views.get_cutting_form, name='get_cutting'),
    path('get-packing/', views.get_packing_form, name='get_packing'),

    # Cutting Sessions
    path('load-cutting-state/', views.load_cutting_state, name='load_cutting_state'),
    path('load-cutting-state/<str:machine_no>/', views.load_cutting_state, name='load_cutting_state_machine'),
    path('start-cutting-session/', views.start_cutting_session, name='start_cutting_session'),
    path('log-cut-roll/', views.log_cut_roll, name='log_cut_roll'),
    path('stop-cutting-session/<int:session_id>/', views.stop_cutting_session, name='stop_cutting_session'),
    path('cutting/complete/<int:session_id>/', views.complete_cutting_roll, name='complete_cutting_roll'),

    # Stateful Extrusion Sessions
    path('load-machine-state/', views.load_machine_state, name='load_machine_state'),
    path('load-machine-state/<str:machine_no>/', views.load_machine_state, name='load_machine_state_machine'),
    path('start-session/', views.start_extrusion_session, name='start_session'),
    path('log-session-roll/', views.log_session_roll, name='log_session_roll'),
    path('session/stop/<int:session_id>/', views.stop_extrusion_session, name='stop_extrusion_session'),
    path('extrusion/complete/<int:session_id>/', views.complete_extrusion_session, name='complete_extrusion_session'),
    path('extrusion/handover/<int:session_id>/', views.handover_extrusion_shift, name='handover_extrusion_shift'),
    path('extrusion/rollover/<int:session_id>/', views.rollover_extrusion_session, name='rollover_extrusion_session'),
    path('extrusion/purge/<int:session_id>/', views.purge_and_close_session, name='purge_and_close_session'),

    # Form Submissions 
    path('submit-packing/', views.submit_packing, name='submit_packing'),
    path('submit-material-usage/', views.submit_material_usage, name='submit_material_usage'),

    # Utilities (Search & Digital Spec Sheets)
    path('get-job-specs/<int:jo_id>/', views.get_job_specs, name='get_job_specs'),
    path('search-jobs/', views.search_jobs, name='search_jobs'),
    path('add-material-row/', views.add_material_row, name='add_material_row'),
    path('get-materials-by-category/', views.get_materials_by_category, name='get_materials_by_category'),
    path('force-close-job/<int:jo_id>/', views.force_close_job, name='force_close_job'),
]