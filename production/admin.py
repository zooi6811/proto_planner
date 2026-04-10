from django.contrib import admin
from .models import (
    MaterialCategory, RawMaterial, MaterialRestockLog, Recipe, RecipeItem,
    JobOrder, MaterialAllocation, MaterialUsageLog, ExtrusionSession,
    SessionMaterial, ExtrusionLog, CuttingLog, PackingLog, DispatchLog
)

# -----------------------------------------------------------------------------
# INVENTORY & MASTER DATA ADMIN
# -----------------------------------------------------------------------------

@admin.register(MaterialCategory)
class MaterialCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'description')
    search_fields = ('name',)

@admin.register(RawMaterial)
class RawMaterialAdmin(admin.ModelAdmin):
    list_display = ('material_id', 'name', 'category', 'current_stock_kg', 'reorder_point_kg')
    list_filter = ('category',)
    search_fields = ('material_id', 'name')
    
    # BUG FIX #14: Protect the live warehouse stock from manual overwrites
    readonly_fields = ('current_stock_kg',)

@admin.register(MaterialRestockLog)
class MaterialRestockLogAdmin(admin.ModelAdmin):
    list_display = ('material', 'amount_kg', 'arrival_date', 'supplier', 'recorded_by')
    list_filter = ('arrival_date', 'material')
    search_fields = ('material__name', 'supplier', 'po_number')
    readonly_fields = ('arrival_date',)

class RecipeItemInline(admin.TabularInline):
    model = RecipeItem
    extra = 1

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
    # BUG FIX #14: Actual usage is driven by floor logs, lock it down here
    readonly_fields = ('actual_used_kg',) 

@admin.register(JobOrder)
class JobOrderAdmin(admin.ModelAdmin):
    list_display = ('jo_number', 'customer', 'order_quantity_kg', 'is_completed', 'extrusion_progress')
    list_filter = ('is_completed', 'target_delivery_date')
    search_fields = ('jo_number', 'customer', 'po_number')
    
    # BUG FIX #14: Lock down all calculated progression metrics
    readonly_fields = (
        'total_est_material_kg', 
        'total_extruded_kg', 
        'total_cut_kg', 
        'total_cutting_wastage_kg', 
        'total_packed_kg', 
        'total_shipped_kg'
    )
    inlines = [MaterialAllocationInline]

@admin.register(MaterialUsageLog)
class MaterialUsageLogAdmin(admin.ModelAdmin):
    list_display = ('job_order', 'material', 'amount_kg', 'is_substitution', 'operator_name', 'timestamp')
    list_filter = ('is_substitution', 'timestamp', 'material')
    search_fields = ('job_order__jo_number', 'operator_name')
    readonly_fields = ('timestamp',)

# -----------------------------------------------------------------------------
# PRODUCTION FLOOR LOGS ADMIN
# -----------------------------------------------------------------------------

class SessionMaterialInline(admin.TabularInline):
    model = SessionMaterial
    extra = 0
    readonly_fields = ('actual_used_kg',)

@admin.register(ExtrusionSession)
class ExtrusionSessionAdmin(admin.ModelAdmin):
    list_display = ('machine_no', 'job_order', 'shift', 'status', 'target_amount_kg', 'total_output_kg')
    list_filter = ('status', 'shift', 'machine_no')
    search_fields = ('machine_no', 'job_order__jo_number', 'operator_name')
    
    # BUG FIX #14: Prevent admin tampering with live session totals
    readonly_fields = ('total_output_kg', 'total_wastage_kg', 'start_time', 'end_time')
    inlines = [SessionMaterialInline]

@admin.register(ExtrusionLog)
class ExtrusionLogAdmin(admin.ModelAdmin):
    list_display = ('session', 'roll_weight_kg', 'wastage_kg', 'timestamp')
    readonly_fields = ('timestamp',)

@admin.register(CuttingLog)
class CuttingLogAdmin(admin.ModelAdmin):
    list_display = ('job_order', 'machine_no', 'shift', 'output_kg', 'wastage_kg', 'operator_name')
    list_filter = ('shift', 'machine_no')
    search_fields = ('job_order__jo_number', 'operator_name')
    readonly_fields = ('timestamp',)

@admin.register(PackingLog)
class PackingLogAdmin(admin.ModelAdmin):
    list_display = ('job_order', 'packing_size_kg', 'quantity_packed', 'operator_name')
    search_fields = ('job_order__jo_number', 'operator_name')
    readonly_fields = ('timestamp',)

@admin.register(DispatchLog)
class DispatchLogAdmin(admin.ModelAdmin):
    list_display = ('job_order', 'shipped_kg', 'delivery_order_no', 'dispatch_date')
    search_fields = ('job_order__jo_number', 'delivery_order_no')
    readonly_fields = ('dispatch_date',)