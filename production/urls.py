from django.urls import path, re_path
from . import views
from django.contrib.auth.views import LogoutView

urlpatterns = [
    # Your gateway login
    path('login/', views.gateway_login, name='login'),
    
    # The crucial logout override (notice the next_page parameter)
    path('logout/', LogoutView.as_view(next_page='login'), name='logout'),
    
    # Main Dashboards
    path('', views.operator_dashboard, name='dashboard'),
    path('control-tower/', views.control_tower, name='control_tower'),
    
    # HTMX Tab Fetching (Standard GET requests safely tolerate redirects)
    path('get-extrusion/', views.get_extrusion_form, name='get_extrusion'),
    path('get-cutting/', views.get_cutting_form, name='get_cutting'),
    path('get-packing/', views.get_packing_form, name='get_packing'),

    # Stateful Extrusion Sessions (Bulletproofed against 301s)
    # The regex allows 'machine_no' to be natively captured whilst remaining tolerant of empty paths
    re_path(r'^load-machine-state(?:/(?P<machine_no>[A-Za-z0-9_-]+))?/?$', views.load_machine_state, name='load_machine_state'),
    re_path(r'^start-session/?$', views.start_extrusion_session, name='start_session'),
    re_path(r'^log-session-roll/?$', views.log_session_roll, name='log_session_roll'),
    re_path(r'^stop-session/(?P<session_id>\d+)/?$', views.stop_extrusion_session, name='stop_session'),

    # Form Submissions (Immune to the HTMX Trailing Slash POST Trap)
    re_path(r'^submit-cutting/?$', views.submit_cutting, name='submit_cutting'),
    re_path(r'^submit-packing/?$', views.submit_packing, name='submit_packing'),
    re_path(r'^submit-material-usage/?$', views.submit_material_usage, name='submit_material_usage'), # Restored missing route

    # Utilities (Search & Digital Spec Sheets)
    re_path(r'^get-job-specs/(?P<jo_id>\d+)/?$', views.get_job_specs, name='get_job_specs'),
    path('search-jobs/', views.search_jobs, name='search_jobs'),

    re_path(r'^add-material-row/?$', views.add_material_row, name='add_material_row'),
    re_path(r'^get-materials-by-category/?$', views.get_materials_by_category, name='get_materials_by_category'),
]