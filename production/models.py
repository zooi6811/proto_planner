from django.db import models, transaction
from django.db.models import F
from decimal import Decimal
from django.core.validators import MinValueValidator

# -----------------------------------------------------------------------------
# MASTER DATA & INVENTORY
# -----------------------------------------------------------------------------

class RawMaterial(models.Model):
    material_id = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    current_stock_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    reorder_point_kg = models.DecimalField(max_digits=10, decimal_places=2, default=100)

    def __str__(self):
        return f"{self.material_id} - {self.name}"

class Recipe(models.Model):
    formula_code = models.CharField(max_length=50, unique=True)
    description = models.CharField(max_length=200, blank=True)
    
    def __str__(self):
        return self.formula_code

class RecipeItem(models.Model):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='ingredients')
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE)
    ratio = models.DecimalField(max_digits=5, decimal_places=4) # e.g., 0.8000 for 80%

    def __str__(self):
        return f"{self.recipe.formula_code} -> {self.material.name} ({self.ratio * 100}%)"

# -----------------------------------------------------------------------------
# JOB ORDER MANAGEMENT
# -----------------------------------------------------------------------------

class JobOrder(models.Model):
    jo_number = models.CharField(max_length=20, unique=True)
    customer = models.CharField(max_length=100)
    
    # Dimensional Data
    product_dimension = models.CharField(max_length=100, default="", help_text="e.g., 230 x 240 x 0.03")
    recipe = models.ForeignKey(Recipe, on_delete=models.SET_NULL, null=True, blank=True)
    wastage_buffer_percent = models.DecimalField(max_digits=5, decimal_places=2, default=10.00)
    
    # Order Targets
    order_quantity_kg = models.DecimalField(max_digits=10, decimal_places=2)
    total_est_material_kg = models.DecimalField(max_digits=10, decimal_places=2, editable=False, default=0)
    
    # Live Tracking Data
    total_extruded_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_cut_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_packed_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    def save(self, *args, **kwargs):
        # Auto-calculate the estimated material requirement
        if self.order_quantity_kg:
            buffer_multiplier = 1 + (self.wastage_buffer_percent / 100)
            self.total_est_material_kg = float(self.order_quantity_kg) * float(buffer_multiplier)
        super().save(*args, **kwargs)
        
    @property
    def extrusion_progress(self):
        if self.order_quantity_kg > 0:
            return round((self.total_extruded_kg / self.order_quantity_kg) * 100, 1)
        return 0

    def __str__(self):
        return f"JO: {self.jo_number} - {self.customer}"

# -----------------------------------------------------------------------------
# FLOOR PRODUCTION LOGS
# -----------------------------------------------------------------------------

class ExtrusionLog(models.Model):
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='extrusion_logs')
    machine_no = models.CharField(max_length=10)
    shift = models.CharField(max_length=10, choices=[('AM', 'Morning'), ('PM', 'Night')])
    timestamp = models.DateTimeField(auto_now_add=True)
    operator_name = models.CharField(max_length=50)
    
    roll_weight_kg = models.DecimalField(
        max_digits=8, decimal_places=2, validators=[MinValueValidator(0.01)]
    )
    wastage_kg = models.DecimalField(
        max_digits=8, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )

    def save(self, *args, **kwargs):
        is_new = self.pk is None 
        super().save(*args, **kwargs)

        if is_new:
            # 1. Update JO Progress
            JobOrder.objects.filter(pk=self.job_order.pk).update(
                total_extruded_kg=F('total_extruded_kg') + self.roll_weight_kg
            )

            # 2. Live Inventory Deduction
            job_order = self.job_order
            if job_order.recipe:
                total_usage_kg = float(self.roll_weight_kg) + float(self.wastage_kg)

                with transaction.atomic():
                    for recipe_item in job_order.recipe.ingredients.all():
                        material = recipe_item.material
                        deduction_amount = total_usage_kg * float(recipe_item.ratio)
                        
                        material.current_stock_kg -= Decimal(str(deduction_amount))
                        material.save()

                        if material.current_stock_kg <= material.reorder_point_kg:
                            print(f"⚠️ ADMIN ALERT: {material.name} stock has dropped to {material.current_stock_kg}kg!")


class CuttingLog(models.Model):
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='cutting_logs')
    machine_no = models.CharField(max_length=10)
    shift = models.CharField(max_length=10, choices=[('AM', 'Morning'), ('PM', 'Night')])
    timestamp = models.DateTimeField(auto_now_add=True)
    operator_name = models.CharField(max_length=50)
    
    output_kg = models.DecimalField(
        max_digits=8, decimal_places=2, validators=[MinValueValidator(0.01)]
    )
    wastage_kg = models.DecimalField(
        max_digits=8, decimal_places=2, default=0, validators=[MinValueValidator(0)]
    )

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            JobOrder.objects.filter(pk=self.job_order.pk).update(
                total_cut_kg=F('total_cut_kg') + self.output_kg
            )

class PackingLog(models.Model):
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='packing_logs')
    timestamp = models.DateTimeField(auto_now_add=True)
    operator_name = models.CharField(max_length=50)
    
    packing_size_kg = models.DecimalField(
        max_digits=6, decimal_places=2, validators=[MinValueValidator(0.01)], help_text="KG per Bag/Pallet"
    )
    quantity_packed = models.IntegerField(
        validators=[MinValueValidator(1)], help_text="Number of Bags/Pallets"
    )

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            total_weight = float(self.packing_size_kg) * float(self.quantity_packed)
            JobOrder.objects.filter(pk=self.job_order.pk).update(
                total_packed_kg=F('total_packed_kg') + total_weight
            )