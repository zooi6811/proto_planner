from django.contrib import admin
from .models import (
    UserProfile, MaterialCategory, RawMaterial, MaterialRestockLog, Recipe, RecipeItem,
    JobOrder, MaterialAllocation, MaterialUsageLog, ExtrusionSession,
    SessionMaterial, ExtrusionLog, CuttingLog, PackingLog, DispatchLog, CuttingSession
)

# -----------------------------------------------------------------------------
# USER MANAGEMENT ADMIN
# -----------------------------------------------------------------------------

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'pin_code')
    list_filter = ('role',)
    search_fields = ('user__username', 'pin_code')

# -----------------------------------------------------------------------------
# INVENTORY & MASTER DATA ADMIN
# -----------------------------------------------------------------------------

@admin.register(MaterialCategory)
class MaterialCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'description')
    search_fields = ('name',)

    # This hides the model from the main admin index menu
    def has_module_permission(self, request):
        return False

# 1. Create Inlines for all Material-related logs
class MaterialRestockLogInline(admin.TabularInline):
    model = MaterialRestockLog
    extra = 1
    readonly_fields = ('arrival_date',)
    # Allows staff to add new restock shipments directly from the Material page

class MaterialUsageLogInline(admin.TabularInline):
    model = MaterialUsageLog
    extra = 0
    # Usage logs are strictly driven by the factory floor, so they are read-only here
    readonly_fields = ('job_order', 'amount_kg', 'is_substitution', 'operator_name', 'timestamp')
    can_delete = False
    
    def has_add_permission(self, request, obj):
        return False

class MaterialAllocationInlineForMaterial(admin.TabularInline):
    model = MaterialAllocation
    extra = 0
    verbose_name = "Job Order Allocation"
    verbose_name_plural = "Job Order Allocations"
    readonly_fields = ('job_order', 'required_kg', 'allocated_kg', 'shortfall_kg', 'actual_used_kg')
    can_delete = False
    
    def has_add_permission(self, request, obj):
        return False

# 2. Attach the Inlines to the main Raw Material Admin
@admin.register(RawMaterial)
class RawMaterialAdmin(admin.ModelAdmin):
    list_display = ('material_id', 'name', 'category', 'current_stock_kg', 'reorder_point_kg')
    list_filter = ('category',)
    search_fields = ('material_id', 'name')
    readonly_fields = ('current_stock_kg',)
    
    # This groups all material history directly onto the material's detail page
    inlines = [
        MaterialRestockLogInline, 
        MaterialAllocationInlineForMaterial, 
        MaterialUsageLogInline
    ]

# (Optional but Recommended) Remove the standalone Restock Log registration 
# so it doesn't clutter the main dashboard menu. We removed the @admin.register(MaterialRestockLog) 
# because it is now cleanly managed within the RawMaterialAdmin above.

class RecipeItemInline(admin.TabularInline):
    model = RecipeItem
    extra = 1
    autocomplete_fields = ['material']

@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    list_display = ('formula_code', 'description')
    search_fields = ('formula_code',)
    inlines = [RecipeItemInline]

# -----------------------------------------------------------------------------
# JOB ORDER & ALLOCATION ADMIN
# -----------------------------------------------------------------------------

class MaterialAllocationInline(admin.TabularInline):
    model = MaterialAllocation
    extra = 0
    readonly_fields = ('actual_used_kg',)
    autocomplete_fields = ['material']

@admin.register(JobOrder)
class JobOrderAdmin(admin.ModelAdmin):
    # Added queue_position to the list display
    list_display = (
        'jo_number', 'customer', 'queue_position', 'order_quantity_kg', 
        'is_completed', 'extrusion_progress', 'cutting_progress', 'packing_progress'
    )
    
    # This allows you to type numbers directly into the list view and click "Save"
    list_editable = ('queue_position',)
    
    list_filter = ('is_completed', 'target_delivery_date')
    search_fields = ('jo_number', 'customer', 'po_number')
    
    readonly_fields = (
        'total_est_material_kg', 'total_extruded_kg', 'total_cut_kg', 
        'total_cutting_wastage_kg', 'total_packed_kg', 'total_shipped_kg'
    )
    inlines = [MaterialAllocationInline]
    actions = ['mark_as_completed']

    fieldsets = (
        ('Basic Information', {
            'fields': ('jo_number', 'customer', 'po_number', 'target_delivery_date', 'queue_position', 'is_completed')
        }),
        ('Production Specifications', {
            'fields': ('product_dimension', 'recipe', 'order_quantity_kg', 'wastage_buffer_percent', 'total_est_material_kg')
        }),
        ('Operator Instructions', {
            'fields': ('printing_required', 'sealing_required', 'slitting_required', 'remarks')
        }),
        ('Progress Tracking (Read-Only)', {
            'fields': ('total_extruded_kg', 'total_cut_kg', 'total_cutting_wastage_kg', 'total_packed_kg', 'total_shipped_kg'),
            'classes': ('collapse',)
        }),
    )

    @admin.action(description='Mark selected job orders as completed (Triggers material refund)')
    def mark_as_completed(self, request, queryset):
        for job in queryset:
            job.complete_job()
        self.message_user(request, f"{queryset.count()} job orders successfully processed and completed.")

# @admin.register(MaterialUsageLog)
# class MaterialUsageLogAdmin(admin.ModelAdmin):
#     list_display = ('job_order', 'material', 'amount_kg', 'is_substitution', 'operator_name', 'timestamp')
#     list_filter = ('is_substitution', 'timestamp', 'material')
#     search_fields = ('job_order__jo_number', 'operator_name')
#     readonly_fields = ('timestamp',)
#     autocomplete_fields = ['job_order', 'material']

# -----------------------------------------------------------------------------
# PRODUCTION FLOOR LOGS ADMIN
# -----------------------------------------------------------------------------

class SessionMaterialInline(admin.TabularInline):
    model = SessionMaterial
    extra = 0
    readonly_fields = ('actual_used_kg',)
    autocomplete_fields = ['material']

@admin.register(ExtrusionSession)
class ExtrusionSessionAdmin(admin.ModelAdmin):
    list_display = ('machine_no', 'job_order', 'shift', 'status', 'target_amount_kg', 'total_output_kg')
    list_filter = ('status', 'shift', 'machine_no')
    search_fields = ('machine_no', 'job_order__jo_number', 'operator_name')
    readonly_fields = ('total_output_kg', 'total_wastage_kg', 'start_time', 'end_time')
    inlines = [SessionMaterialInline]
    autocomplete_fields = ['job_order']

@admin.register(ExtrusionLog)
class ExtrusionLogAdmin(admin.ModelAdmin):
    list_display = ('session', 'roll_weight_kg', 'wastage_kg', 'timestamp')
    readonly_fields = ('timestamp',)
    autocomplete_fields = ['session']

# -----------------------------------------------------------------------------
# CUTTING LOGS & SESSIONS ADMIN
# -----------------------------------------------------------------------------
class CuttingLogInline(admin.TabularInline):
    model = CuttingLog
    extra = 0
    readonly_fields = ('timestamp',)

@admin.register(CuttingSession)
class CuttingSessionAdmin(admin.ModelAdmin):
    list_display = ('machine_no', 'job_order', 'shift', 'status', 'input_roll_weight_kg', 'total_output_kg', 'total_wastage_kg')
    list_filter = ('status', 'shift', 'machine_no')
    search_fields = ('machine_no', 'job_order__jo_number', 'operator_name')
    readonly_fields = ('total_output_kg', 'total_wastage_kg', 'start_time', 'end_time')
    inlines = [CuttingLogInline]
    autocomplete_fields = ['job_order']

@admin.register(CuttingLog)
class CuttingLogAdmin(admin.ModelAdmin):
    list_display = ('session', 'output_kg', 'timestamp')
    readonly_fields = ('timestamp',)
    autocomplete_fields = ['session']

@admin.register(PackingLog)
class PackingLogAdmin(admin.ModelAdmin):
    list_display = ('job_order', 'packing_size_kg', 'quantity_packed', 'operator_name')
    search_fields = ('job_order__jo_number', 'operator_name')
    readonly_fields = ('timestamp',)
    autocomplete_fields = ['job_order']

@admin.register(DispatchLog)
class DispatchLogAdmin(admin.ModelAdmin):
    list_display = ('job_order', 'shipped_kg', 'delivery_order_no', 'dispatch_date')
    search_fields = ('job_order__jo_number', 'delivery_order_no')
    readonly_fields = ('dispatch_date',)
    autocomplete_fields = ['job_order']