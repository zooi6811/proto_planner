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
    ratio = models.DecimalField(max_digits=5, decimal_places=4)

    def __str__(self):
        return f"{self.recipe.formula_code} -> {self.material.name} ({self.ratio * 100}%)"
    
# -----------------------------------------------------------------------------
# MATERIAL ALLOCATION & ISSUANCE
# -----------------------------------------------------------------------------
class MaterialAllocation(models.Model):
    job_order = models.ForeignKey('JobOrder', on_delete=models.CASCADE, related_name='allocations')
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE)
    
    required_kg = models.DecimalField(max_digits=10, decimal_places=2)
    allocated_kg = models.DecimalField(max_digits=10, decimal_places=2, help_text="Physical stock reserved for this job")
    shortfall_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Hypothetical stock (Needs Purchasing)")
    
    actual_used_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    @property
    def is_overused(self):
        # Warn if actual usage exceeds the required amount + a 2% leeway
        return float(self.actual_used_kg) > (float(self.required_kg) * 1.02)

    def __str__(self):
        return f"{self.job_order.jo_number} - {self.material.name} (Shortfall: {self.shortfall_kg} KG)"
    
class MaterialUsageLog(models.Model):
    job_order = models.ForeignKey('JobOrder', on_delete=models.CASCADE, related_name='usage_logs')
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE)
    amount_kg = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0.01)])
    timestamp = models.DateTimeField(auto_now_add=True)
    operator_name = models.CharField(max_length=50)
    
    # Flags if this was an off-recipe substitution
    is_substitution = models.BooleanField(default=False) 

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)

        if is_new:
            with transaction.atomic():
                # 1. Fetch or create the allocation record
                allocation, created = MaterialAllocation.objects.get_or_create(
                    job_order=self.job_order,
                    material=self.material,
                    defaults={
                        'required_kg': 0, 'allocated_kg': 0, 'shortfall_kg': 0, 'actual_used_kg': 0
                    }
                )

                # 2. Identify if this is an Ad-Hoc Substitution
                if created or allocation.required_kg == 0:
                    self.is_substitution = True
                    super().save(update_fields=['is_substitution']) # Update the flag silently
                    
                    # Because it wasn't pre-allocated, we MUST deduct it from live warehouse stock now
                    live_material = RawMaterial.objects.select_for_update().get(pk=self.material.pk)
                    live_material.current_stock_kg -= Decimal(str(self.amount_kg))
                    live_material.save()
                    
                    if live_material.current_stock_kg <= live_material.reorder_point_kg:
                        print(f"⚠️ ADMIN ALERT: {live_material.name} stock has dropped due to an unexpected substitution!")

                # 3. Log the actual usage against the Job Order
                allocation.actual_used_kg = float(allocation.actual_used_kg) + float(self.amount_kg)
                allocation.save()

# -----------------------------------------------------------------------------
# JOB ORDER MANAGEMENT
# -----------------------------------------------------------------------------

class JobOrder(models.Model):
    
    jo_number = models.CharField(max_length=20, unique=True)
    customer = models.CharField(max_length=100)
    
    # Digital Production Form Specifications
    po_number = models.CharField(max_length=50, blank=True, default="-")
    target_delivery_date = models.DateField(null=True, blank=True)
    product_dimension = models.CharField(max_length=100, default="", help_text="e.g., 230 x 240 x 0.03")
    recipe = models.ForeignKey(Recipe, on_delete=models.SET_NULL, null=True, blank=True)
    
    # Operator Instructions
    printing_required = models.BooleanField(default=False)
    sealing_required = models.BooleanField(default=False)
    slitting_required = models.BooleanField(default=False)
    remarks = models.TextField(blank=True, default="-")
    
    # Targets & Progress
    wastage_buffer_percent = models.DecimalField(max_digits=5, decimal_places=2, default=10.00)
    order_quantity_kg = models.DecimalField(max_digits=10, decimal_places=2)
    total_est_material_kg = models.DecimalField(max_digits=10, decimal_places=2, editable=False, default=0)
    
    total_extruded_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_cut_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_packed_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    is_completed = models.BooleanField(default=False, help_text="Mark as true when the entire order is finished.")

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        
        # 1. Calculate Estimated Total Material Required
        if self.order_quantity_kg:
            buffer_multiplier = 1 + (float(self.wastage_buffer_percent) / 100)
            self.total_est_material_kg = float(self.order_quantity_kg) * float(buffer_multiplier)
            
        super().save(*args, **kwargs)
        
        # 2. Upfront Material Allocation (Only runs when a JO is first created)
        if is_new and self.recipe:
            with transaction.atomic():
                for recipe_item in self.recipe.ingredients.all():
                    # Lock the material row to prevent concurrent booking conflicts
                    material = RawMaterial.objects.select_for_update().get(pk=recipe_item.material.pk)
                    
                    # Calculate exact KG needed for this specific ingredient
                    required_amount = Decimal(str(self.total_est_material_kg * float(recipe_item.ratio)))
                    
                    allocated_amount = Decimal('0.00')
                    shortfall_amount = Decimal('0.00')

                    # Check against physical warehouse stock
                    if material.current_stock_kg >= required_amount:
                        allocated_amount = required_amount
                        material.current_stock_kg -= required_amount
                    else:
                        allocated_amount = material.current_stock_kg
                        shortfall_amount = required_amount - material.current_stock_kg
                        material.current_stock_kg = Decimal('0.00') # Drain remaining stock
                    
                    material.save()

                    # Generate the Digital Allocation Ticket
                    MaterialAllocation.objects.create(
                        job_order=self,
                        material=material,
                        required_kg=required_amount,
                        allocated_kg=allocated_amount,
                        shortfall_kg=shortfall_amount
                    )
        
    @property
    def extrusion_progress(self):
        if self.order_quantity_kg > 0:
            return round((self.total_extruded_kg / self.order_quantity_kg) * 100, 1)
        return 0

    def __str__(self):
        return f"JO: {self.jo_number} - {self.customer}"
    
    def complete_job(self):
        """
        To be called by the Admin when closing a job. 
        Refunds any allocated material that was NOT actually used.
        """
        if self.is_completed:
            return # Prevent double-refunding
            
        with transaction.atomic():
            for allocation in self.allocations.all():
                unused_kg = float(allocation.allocated_kg) - float(allocation.actual_used_kg)
                
                # If they used less than allocated (e.g., due to a substitution), refund the rest
                if unused_kg > 0:
                    material = RawMaterial.objects.select_for_update().get(pk=allocation.material.pk)
                    material.current_stock_kg += Decimal(str(unused_kg))
                    material.save()
                    
                    # Zero out the remaining allocation so the ledger balances
                    allocation.allocated_kg = allocation.actual_used_kg
                    allocation.save()
            
            self.is_completed = True
            self.save(update_fields=['is_completed'])

# -----------------------------------------------------------------------------
# FLOOR PRODUCTION LOGS
# -----------------------------------------------------------------------------

class ExtrusionLog(models.Model):
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='extrusion_logs')
    machine_no = models.CharField(max_length=10)
    shift = models.CharField(max_length=10, choices=[('AM', 'Morning'), ('PM', 'Night')])
    timestamp = models.DateTimeField(auto_now_add=True)
    operator_name = models.CharField(max_length=50)
    
    roll_weight_kg = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(0.01)])
    wastage_kg = models.DecimalField(max_digits=8, decimal_places=2, default=0, validators=[MinValueValidator(0)])

    def save(self, *args, **kwargs):
        is_new = self.pk is None 
        super().save(*args, **kwargs)

        if is_new:
            # Only update the Job Order's progress. 
            # (Material deductions are now handled by MaterialAllocation & MaterialUsageLogs)
            JobOrder.objects.filter(pk=self.job_order.pk).update(
                total_extruded_kg=F('total_extruded_kg') + self.roll_weight_kg
            )

class CuttingLog(models.Model):
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='cutting_logs')
    machine_no = models.CharField(max_length=10)
    shift = models.CharField(max_length=10, choices=[('AM', 'Morning'), ('PM', 'Night')])
    timestamp = models.DateTimeField(auto_now_add=True)
    operator_name = models.CharField(max_length=50)
    
    output_kg = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(0.01)])
    wastage_kg = models.DecimalField(max_digits=8, decimal_places=2, default=0, validators=[MinValueValidator(0)])

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
    
    packing_size_kg = models.DecimalField(max_digits=6, decimal_places=2, validators=[MinValueValidator(0.01)], help_text="KG per Bag/Pallet")
    quantity_packed = models.IntegerField(validators=[MinValueValidator(1)], help_text="Number of Bags/Pallets")

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            total_weight = float(self.packing_size_kg) * float(self.quantity_packed)
            JobOrder.objects.filter(pk=self.job_order.pk).update(
                total_packed_kg=F('total_packed_kg') + total_weight
            )