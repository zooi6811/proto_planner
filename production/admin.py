from django.contrib import admin
from .models import (
    RawMaterial, 
    Recipe, 
    RecipeItem, 
    JobOrder, 
    ExtrusionLog, 
    CuttingLog, 
    PackingLog,
    MaterialAllocation
)

# Registering the models allows the Admin to view, add, edit, and delete records
admin.site.register(RawMaterial)
admin.site.register(Recipe)
admin.site.register(RecipeItem)
admin.site.register(JobOrder)
admin.site.register(ExtrusionLog)
admin.site.register(CuttingLog)
admin.site.register(PackingLog)
admin.site.register(MaterialAllocation)