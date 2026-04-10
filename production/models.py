from django.db import models, transaction
from django.db.models import F
from decimal import Decimal
from django.core.validators import MinValueValidator
from django.utils import timezone
from django.core.exceptions import ValidationError

# -----------------------------------------------------------------------------
# MASTER DATA & INVENTORY
# -----------------------------------------------------------------------------

class MaterialCategory(models.Model):
    name = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "Material Categories"

    def __str__(self):
        return self.name

class RawMaterial(models.Model):
    category = models.ForeignKey(MaterialCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='materials')
    material_id = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    current_stock_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    reorder_point_kg = models.DecimalField(max_digits=10, decimal_places=2, default=100)

    def __str__(self):
        return f"{self.material_id} - {self.name}"

class MaterialRestockLog(models.Model):
    """Logs incoming shipments of raw materials and automatically updates inventory."""
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE, related_name='restocks')
    arrival_date = models.DateTimeField(auto_now_add=True)
    amount_kg = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0.01)])
    
    # Optional tracking fields
    supplier = models.CharField(max_length=100, blank=True, default="-")
    po_number = models.CharField(max_length=50, blank=True, default="-")
    recorded_by = models.CharField(max_length=50, default="Admin")

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        
        # Only add to stock when the log is first created, not if it's merely updated
        if is_new:
            with transaction.atomic():
                mat = RawMaterial.objects.select_for_update().get(pk=self.material.pk)
                mat.current_stock_kg += Decimal(str(self.amount_kg))
                mat.save()

    def __str__(self):
        return f"+{self.amount_kg} KG of {self.material.name} on {self.arrival_date.strftime('%Y-%m-%d')}"

class Recipe(models.Model):
    formula_code = models.CharField(max_length=50, unique=True)
    description = models.CharField(max_length=200, blank=True)
    
    def __str__(self):
        return self.formula_code

class RecipeItem(models.Model):
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='ingredients')
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE)
    ratio = models.DecimalField(max_digits=5, decimal_places=4)

    def clean(self):
        if self.ratio <= 0 or self.ratio > 1:
            raise ValidationError({'ratio': 'Ratio must be strictly between 0 and 1 (0% to 100%).'})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.recipe.formula_code} -> {self.material.name} ({self.ratio * 100}%)"

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
    total_cutting_wastage_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_extruded_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_cut_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_packed_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    # Fulfilment & Shipping
    total_shipped_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_completed = models.BooleanField(default=False, help_text="Mark as true when the entire order is finished.")

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        
        # Calculate Estimated Total Material Required based on wastage buffer
        if self.order_quantity_kg:
            buffer_multiplier = 1 + (float(self.wastage_buffer_percent) / 100)
            self.total_est_material_kg = float(self.order_quantity_kg) * float(buffer_multiplier)
            
        super().save(*args, **kwargs)
        
        # Upfront Material Allocation (Only runs when a JO is first created)
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

    def complete_job(self):
        """
        To be called by the Admin when closing a job. 
        Refunds any allocated material that was NOT actually used.
        """
        if self.is_completed:
            return 
            
        with transaction.atomic():
            for allocation in self.allocations.all():
                unused_kg = float(allocation.allocated_kg) - float(allocation.actual_used_kg)
                
                # If they used less than allocated, refund the remainder to warehouse
                if unused_kg > 0:
                    material = RawMaterial.objects.select_for_update().get(pk=allocation.material.pk)
                    material.current_stock_kg += Decimal(str(unused_kg))
                    material.save()
                    
                    # Zero out the remaining allocation so the ledger balances
                    allocation.allocated_kg = allocation.actual_used_kg
                    allocation.save()
            
            self.is_completed = True
            self.save(update_fields=['is_completed'])

    @property
    def extrusion_progress(self):
        if self.order_quantity_kg > 0:
            return round((float(self.total_extruded_kg) / float(self.order_quantity_kg)) * 100, 1)
        return 0

    @property
    def order_balance_kg(self):
        # How much is left to produce and ship to fulfil the customer's request
        return float(self.order_quantity_kg) - float(self.total_shipped_kg)

    @property
    def ready_to_ship_kg(self):
        # Goods packed and waiting in the warehouse
        return float(self.total_packed_kg) - float(self.total_shipped_kg)
    
    @property
    def remaining_extrusion_kg(self):
        """Calculates how much of the original order is left to physically extrude, factoring in material lost to cutting waste."""
        # Calculate the actual 'surviving' material that can be turned into the final product
        usable_extruded = float(self.total_extruded_kg) - float(self.total_cutting_wastage_kg)
        remaining = float(self.order_quantity_kg) - usable_extruded
        return max(0, remaining)

    def __str__(self):
        return f"JO: {self.jo_number} - {self.customer}"

# -----------------------------------------------------------------------------
# MATERIAL ALLOCATION & USAGE
# -----------------------------------------------------------------------------

class MaterialAllocation(models.Model):
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='allocations')
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
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='usage_logs')
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE)
    amount_kg = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0.01)])
    timestamp = models.DateTimeField(auto_now_add=True)
    operator_name = models.CharField(max_length=50)
    
    is_substitution = models.BooleanField(default=False) 

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)

        if is_new:
            with transaction.atomic():
                allocation, created = MaterialAllocation.objects.get_or_create(
                    job_order=self.job_order,
                    material=self.material,
                    defaults={
                        'required_kg': 0, 'allocated_kg': 0, 'shortfall_kg': 0, 'actual_used_kg': 0
                    }
                )

                # Identify if this is an Ad-Hoc Substitution
                if created or allocation.required_kg == 0:
                    self.is_substitution = True
                    super().save(update_fields=['is_substitution'])
                    
                    live_material = RawMaterial.objects.select_for_update().get(pk=self.material.pk)
                    live_material.current_stock_kg -= Decimal(str(self.amount_kg))
                    live_material.save()
                    
                    if live_material.current_stock_kg <= live_material.reorder_point_kg:
                        print(f"⚠️ ADMIN ALERT: {live_material.name} stock has dropped due to an unexpected substitution!")

                allocation.actual_used_kg = float(allocation.actual_used_kg) + float(self.amount_kg)
                allocation.save()

# -----------------------------------------------------------------------------
# FLOOR PRODUCTION LOGS (STATEFUL SESSIONS)
# -----------------------------------------------------------------------------

class ExtrusionSession(models.Model):
    """Represents an active run on a specific machine by an operator."""
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='extrusion_sessions')
    machine_no = models.CharField(max_length=10)
    shift = models.CharField(max_length=10, choices=[('AM', 'Morning'), ('PM', 'Night')])
    operator_name = models.CharField(max_length=50, default="Extrusion Op")
    
    # Session Targets
    target_amount_kg = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default='ACTIVE', choices=[('ACTIVE', 'Active'), ('COMPLETED', 'Completed')])
    
    # Time Tracking
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)
    
    # Live Running Totals for this specific session
    total_output_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_wastage_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    def stop_session(self):
        """Calculates actual material used vs output, and refunds the remainder to the warehouse."""
        if self.status != 'ACTIVE':
            return
            
        total_produced = float(self.total_output_kg) + float(self.total_wastage_kg)
        total_reserved = sum(float(m.reserved_kg) for m in self.materials.all())
        
        with transaction.atomic():
            if total_produced < total_reserved and total_reserved > 0:
                # Pro-rata refund of unused reserved materials
                refund_ratio = (total_reserved - total_produced) / total_reserved
                
                for sm in self.materials.all():
                    refund_amount = float(sm.reserved_kg) * refund_ratio
                    sm.actual_used_kg = Decimal(str(float(sm.reserved_kg) - refund_amount))
                    sm.save()
                    
                    # Return unused material to the warehouse stock
                    mat = RawMaterial.objects.select_for_update().get(pk=sm.material.pk)
                    mat.current_stock_kg += Decimal(str(refund_amount))
                    mat.save()
            else:
                # Output matched or exceeded reservation; all reserved material is considered 'used'
                for sm in self.materials.all():
                    sm.actual_used_kg = sm.reserved_kg
                    sm.save()

            self.status = 'COMPLETED'
            self.end_time = timezone.now()
            self.save(update_fields=['status', 'end_time'])

    def __str__(self):
        return f"{self.machine_no} | {self.job_order.jo_number} ({self.status})"

class SessionMaterial(models.Model):
    """The specific materials the operator reserved for this active session."""
    session = models.ForeignKey(ExtrusionSession, on_delete=models.CASCADE, related_name='materials')
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE)
    reserved_kg = models.DecimalField(max_digits=10, decimal_places=2)
    actual_used_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)

class ExtrusionLog(models.Model):
    """Individual rolls produced during an active session."""
    session = models.ForeignKey(ExtrusionSession, on_delete=models.CASCADE, related_name='rolls')
    timestamp = models.DateTimeField(auto_now_add=True)
    
    roll_weight_kg = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(0.01)])
    wastage_kg = models.DecimalField(max_digits=8, decimal_places=2, default=0, validators=[MinValueValidator(0)])

    def clean(self):
        if float(self.wastage_kg) < 0 or float(self.wastage_kg) > float(self.roll_weight_kg):
            raise ValidationError({'wastage_kg': 'Wastage cannot be negative or greater than the total roll weight.'})

    def save(self, *args, **kwargs):
        self.clean()
        is_new = self.pk is None 
        super().save(*args, **kwargs)

        if is_new:
            # 1. Update the Session's running totals
            session = self.session
            session.total_output_kg = float(session.total_output_kg) + float(self.roll_weight_kg)
            session.total_wastage_kg = float(session.total_wastage_kg) + float(self.wastage_kg)
            session.save(update_fields=['total_output_kg', 'total_wastage_kg'])

            # 2. Update the Master Job Order's progress
            JobOrder.objects.filter(pk=session.job_order.pk).update(
                total_extruded_kg=F('total_extruded_kg') + self.roll_weight_kg
            )
            
            # 3. Auto-stop if target is reached
            if session.total_output_kg >= session.target_amount_kg:
                session.stop_session()

class CuttingLog(models.Model):
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='cutting_logs')
    machine_no = models.CharField(max_length=10)
    shift = models.CharField(max_length=10, choices=[('AM', 'Morning'), ('PM', 'Night')])
    timestamp = models.DateTimeField(auto_now_add=True)
    operator_name = models.CharField(max_length=50)
    
    output_kg = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(0.01)])
    wastage_kg = models.DecimalField(max_digits=8, decimal_places=2, default=0, validators=[MinValueValidator(0)])

    def clean(self):
        if self.job_order_id:
            remaining = float(self.job_order.total_extruded_kg) - float(self.job_order.total_cut_kg)
            if self.pk is None and float(self.output_kg) > (remaining * 1.05):
                raise ValidationError({'output_kg': f'Cannot cut {self.output_kg}kg. Only {remaining:.1f}kg remains from Extrusion.'})

    def save(self, *args, **kwargs):
        self.clean()
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

    def clean(self):
        if self.job_order_id:
            remaining = float(self.job_order.total_cut_kg) - float(self.job_order.total_packed_kg)
            total_weight = float(self.packing_size_kg) * float(self.quantity_packed)
            if self.pk is None and total_weight > (remaining * 1.05):
                raise ValidationError({'quantity_packed': f'Attempting to pack {total_weight:.1f}kg, but only {remaining:.1f}kg has been cut and is available.'})

    def save(self, *args, **kwargs):
        self.clean()
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            total_weight = float(self.packing_size_kg) * float(self.quantity_packed)
            JobOrder.objects.filter(pk=self.job_order.pk).update(
                total_packed_kg=F('total_packed_kg') + total_weight
            )

class DispatchLog(models.Model):
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='shipments')
    dispatch_date = models.DateTimeField(auto_now_add=True)
    shipped_kg = models.DecimalField(max_digits=10, decimal_places=2)
    delivery_order_no = models.CharField(max_length=50, blank=True)

    def clean(self):
        if float(self.shipped_kg) <= 0:
            raise ValidationError({'shipped_kg': 'Shipped quantity must be greater than zero.'})
            
        if self.job_order_id:
            remaining = float(self.job_order.total_packed_kg) - float(self.job_order.total_shipped_kg)
            if self.pk is None and float(self.shipped_kg) > remaining:
                raise ValidationError({'shipped_kg': f'Cannot dispatch {self.shipped_kg}kg. Only {remaining:.1f}kg is packed and ready for shipping.'})

    def save(self, *args, **kwargs):
        self.clean()
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            JobOrder.objects.filter(pk=self.job_order.pk).update(
                total_shipped_kg=F('total_shipped_kg') + self.shipped_kg
            )