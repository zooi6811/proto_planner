from django.contrib import admin
from .models import (
    RawMaterial, Recipe, RecipeItem, JobOrder, 
    ExtrusionLog, CuttingLog, PackingLog, 
    MaterialAllocation, MaterialUsageLog, DispatchLog, ExtrusionSession, SessionMaterial
)

# --- INLINES ---
class RecipeItemInline(admin.TabularInline):
    model = RecipeItem
    extra = 1

class MaterialAllocationInline(admin.TabularInline):
    model = MaterialAllocation
    extra = 0
    readonly_fields = ('material', 'required_kg', 'allocated_kg', 'shortfall_kg', 'actual_used_kg', 'is_overused')
    can_delete = False
    
    # Do not allow adding allocations manually here; it is automated on save
    def has_add_permission(self, request, obj=None):
        return False

class DispatchLogInline(admin.TabularInline):
    model = DispatchLog
    extra = 0

# --- ADMIN CLASSES ---
class RecipeAdmin(admin.ModelAdmin):
    inlines = [RecipeItemInline]
    list_display = ('formula_code', 'description')

class JobOrderAdmin(admin.ModelAdmin):
    inlines = [MaterialAllocationInline, DispatchLogInline]
    list_display = ('jo_number', 'customer', 'order_quantity_kg', 'total_extruded_kg', 'order_balance_kg', 'is_completed')
    list_filter = ('is_completed',)

class RawMaterialAdmin(admin.ModelAdmin):
    list_display = ('material_id', 'name', 'current_stock_kg', 'reorder_point_kg')

admin.site.register(RawMaterial, RawMaterialAdmin)
admin.site.register(Recipe, RecipeAdmin)
admin.site.register(JobOrder, JobOrderAdmin)

admin.site.register(ExtrusionLog)
admin.site.register(CuttingLog)
admin.site.register(PackingLog)
admin.site.register(MaterialUsageLog)
admin.site.register(DispatchLog)
admin.site.register(ExtrusionSession)
admin.site.register(SessionMaterial)