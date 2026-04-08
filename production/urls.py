from django.urls import path
from . import views

urlpatterns = [
    path('', views.operator_dashboard, name='dashboard'),

    path('control-tower/', views.control_tower, name='control_tower'),
    
    # Form Submissions
    path('submit-extrusion/', views.submit_extrusion, name='submit_extrusion'),
    path('submit-cutting/', views.submit_cutting, name='submit_cutting'),
    path('submit-packing/', views.submit_packing, name='submit_packing'),
    
    # HTMX Tab Fetching
    path('get-extrusion/', views.get_extrusion_form, name='get_extrusion'),
    path('get-cutting/', views.get_cutting_form, name='get_cutting'),
    path('get-packing/', views.get_packing_form, name='get_packing'),
]