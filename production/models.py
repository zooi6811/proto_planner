from django.db import models, transaction
from django.db.models import Manager, Q, Sum, F, DecimalField
from django.db.models.functions import Coalesce
from decimal import Decimal
from django.core.validators import MinValueValidator
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey

class AuditLog(models.Model):
    """
    Append-only global audit trail for tracking critical manufacturing actions.
    """
    ACTION_CHOICES = [
        ('SESSION_STARTED', 'Session Started'),
        ('SESSION_STOPPED', 'Session Stopped'),
        ('SESSION_PURGED', 'Session Purged & Reconciled'),
        ('MATERIAL_USED', 'Material Used / Deducted'),
        ('OUTPUT_LOGGED', 'Production Output Logged'),
        ('JOB_COMPLETED', 'Job Fully Completed'),
    ]
    
    operator_name = models.CharField(max_length=50, help_text="User or operator who performed the action")
    action_type = models.CharField(max_length=50, choices=ACTION_CHOICES)
    
    # Generic relation to tie the log to ANY model (JobOrder, ExtrusionSession, etc.)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey('content_type', 'object_id')
    
    # Flexible storage for before/after values, reasons, or specific quantities
    details = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['content_type', 'object_id']), # Fast lookup for a specific job/session
            models.Index(fields=['action_type']),
            models.Index(fields=['timestamp']),
        ]

    def __str__(self):
        return f"{self.timestamp.strftime('%Y-%m-%d %H:%M')} | {self.operator_name} | {self.action_type}"

class UserProfile(models.Model):
    ROLE_CHOICES = [
        ('OPERATOR', 'Floor Operator'),
        ('STAFF', 'Management / Staff'),
    ]
    
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='OPERATOR')
    pin_code = models.CharField(max_length=4, unique=True, null=True, blank=True, help_text="4-digit PIN for Operator Terminal access")

    def __str__(self):
        return f"{self.user.username} - {self.get_role_display()}"

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
    current_stock_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    reorder_point_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('100.00'))

    def __str__(self):
        return f"{self.material_id} - {self.name}"

class MaterialRestockLog(models.Model):
    """Logs incoming shipments of raw materials and automatically updates inventory."""
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE, related_name='restocks')
    arrival_date = models.DateTimeField(auto_now_add=True)
    amount_kg = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    
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
                mat.current_stock_kg += self.amount_kg
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
        if self.ratio <= Decimal('0') or self.ratio > Decimal('1'):
            raise ValidationError({'ratio': 'Ratio must be strictly between 0 and 1 (0% to 100%).'})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.recipe.formula_code} -> {self.material.name} ({self.ratio * Decimal('100')}%)"

# -----------------------------------------------------------------------------
# JOB ORDER MANAGEMENT
# -----------------------------------------------------------------------------

class JobOrderManager(Manager):
    """Encapsulates complex pipeline tier queries for the Control Tower."""
    
    def active_jobs(self, limit=10):
        return self.filter(
            Q(extrusion_sessions__status='ACTIVE') | Q(total_extruded_kg__gt=0),
            is_completed=False
        ).select_related('recipe').distinct().order_by('-id')[:limit]

    def queued_jobs(self, limit=10):
        return self.filter(
            is_completed=False, 
            total_extruded_kg=0
        ).exclude(extrusion_sessions__status='ACTIVE').select_related('recipe').order_by('queue_position', 'target_delivery_date', 'id')[:limit]

    def completed_jobs(self, limit=10):
        return self.filter(is_completed=True).select_related('recipe').order_by('-id')[:limit]

class JobOrder(models.Model):
    jo_number = models.CharField(max_length=20, unique=True)
    customer = models.CharField(max_length=100)

    queue_position = models.PositiveIntegerField(
        default=100, 
        help_text="Lower numbers run first (e.g., 1 is top priority). Use 100 for standard/un-queued jobs."
    )
    
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
    total_extrusion_wastage_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    
    total_est_material_kg = models.DecimalField(max_digits=10, decimal_places=2, editable=False, default=Decimal('0.00'))
    total_cutting_wastage_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_extruded_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_cut_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_packed_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_shipped_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    # Fulfilment & Shipping
    is_completed = models.BooleanField(default=False, help_text="Mark as true when the entire order is finished.")

    objects = JobOrderManager()

    class Meta:
        ordering = ['is_completed', 'queue_position', 'target_delivery_date']

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        
        # Calculate Estimated Total Material Required based on wastage buffer using Decimals
        if self.order_quantity_kg:
            buffer_multiplier = Decimal('1.00') + (self.wastage_buffer_percent / Decimal('100.00'))
            self.total_est_material_kg = self.order_quantity_kg * buffer_multiplier
            
        super().save(*args, **kwargs)
        
        # Upfront Material Allocation (Only runs when a JO is first created)
        if is_new and self.recipe:
            with transaction.atomic():
                for recipe_item in self.recipe.ingredients.all():
                    material = RawMaterial.objects.select_for_update().get(pk=recipe_item.material.pk)
                    required_amount = self.total_est_material_kg * recipe_item.ratio
                    
                    allocated_amount = Decimal('0.00')
                    shortfall_amount = Decimal('0.00')

                    if material.current_stock_kg >= required_amount:
                        allocated_amount = required_amount
                        material.current_stock_kg -= required_amount
                    else:
                        allocated_amount = material.current_stock_kg
                        shortfall_amount = required_amount - material.current_stock_kg
                        material.current_stock_kg = Decimal('0.00') 
                    
                    material.save()

                    MaterialAllocation.objects.create(
                        job_order=self,
                        material=material,
                        required_kg=required_amount,
                        allocated_kg=allocated_amount,
                        shortfall_kg=shortfall_amount
                    )

    def complete_job(self):
        """Refunds any allocated material that was NOT actually used upon completion."""
        if self.is_completed:
            return 
            
        with transaction.atomic():
            for allocation in self.allocations.all():
                unused_kg = allocation.allocated_kg - allocation.actual_used_kg
                
                if unused_kg > Decimal('0'):
                    material = RawMaterial.objects.select_for_update().get(pk=allocation.material.pk)
                    material.current_stock_kg += unused_kg
                    material.save()
                    
                    allocation.allocated_kg = allocation.actual_used_kg
                    allocation.save()
            
            self.is_completed = True
            self.save(update_fields=['is_completed'])

    @property
    def extrusion_progress(self):
        if self.order_quantity_kg > Decimal('0'):
            return round((self.total_extruded_kg / self.order_quantity_kg) * Decimal('100'), 1)
        return Decimal('0')
    
    @property
    def cutting_progress(self):
        if self.order_quantity_kg > Decimal('0'):
            return round((self.total_cut_kg / self.order_quantity_kg) * Decimal('100'), 1)
        return Decimal('0')

    @property
    def packing_progress(self):
        if self.order_quantity_kg > Decimal('0'):
            return round((self.total_packed_kg / self.order_quantity_kg) * Decimal('100'), 1)
        return Decimal('0')

    @property
    def order_balance_kg(self):
        return self.order_quantity_kg - self.total_shipped_kg

    @property
    def ready_to_ship_kg(self):
        return self.total_packed_kg - self.total_shipped_kg
    
    @property
    def remaining_extrusion_kg(self):
        usable_extruded = self.total_extruded_kg - self.total_cutting_wastage_kg
        remaining = self.order_quantity_kg - usable_extruded
        return max(Decimal('0'), remaining)
    
    @property
    def extrusion_wastage_pct(self):
        """Calculates blowing/extrusion wastage percentage."""
        total_material_processed = self.total_extruded_kg + self.total_extrusion_wastage_kg
        if total_material_processed > Decimal('0.00'):
            return round((self.total_extrusion_wastage_kg / total_material_processed) * Decimal('100'), 2)
        return Decimal('0.00')

    @property
    def cutting_wastage_pct(self):
        """Calculates cutting/slitting wastage percentage."""
        total_material_processed = self.total_cut_kg + self.total_cutting_wastage_kg
        if total_material_processed > Decimal('0.00'):
            return round((self.total_cutting_wastage_kg / total_material_processed) * Decimal('100'), 2)
        return Decimal('0.00')

    @property
    def overall_wastage_pct(self):
        """Calculates the total factory floor wastage percentage against the hopper input."""
        total_waste = self.total_extrusion_wastage_kg + self.total_cutting_wastage_kg
        total_input = self.total_extruded_kg + self.total_extrusion_wastage_kg 
        
        if total_input > Decimal('0.00'):
            return round((total_waste / total_input) * Decimal('100'), 2)
        return Decimal('0.00')

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
    shortfall_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="Hypothetical stock (Needs Purchasing)")
    actual_used_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    @property
    def is_overused(self):
        return self.actual_used_kg > (self.required_kg * Decimal('1.02'))

    def __str__(self):
        return f"{self.job_order.jo_number} - {self.material.name} (Shortfall: {self.shortfall_kg} KG)"

class MaterialUsageLog(models.Model):
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='usage_logs')
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE)
    amount_kg = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    timestamp = models.DateTimeField(auto_now_add=True)
    operator_name = models.CharField(max_length=50, null=True, blank=True)
    
    is_substitution = models.BooleanField(default=False) 

    # REFACTOR: Removed the complex custom save() method. 
    # Logic moved to a strict classmethod to prevent hidden side-effects during ORM calls.

    @classmethod
    def record_usage(cls, job_order, material, amount_kg, operator_name):
        """
        Service method to handle ad-hoc material usage safely.
        Validates stock and handles allocation logic within a strict atomic lock.
        """
        with transaction.atomic():
            # Lock the job and material to prevent concurrent modifications
            jo = JobOrder.objects.select_for_update().get(pk=job_order.pk)
            live_material = RawMaterial.objects.select_for_update().get(pk=material.pk)

            # BUG FIX: Concurrency-safe stock check inside the atomic block
            if amount_kg > live_material.current_stock_kg:
                raise ValidationError(f"Insufficient stock. You requested {amount_kg}kg, but only {live_material.current_stock_kg}kg of {live_material.name} is available.")

            if jo.is_completed:
                raise ValidationError("This Job Order is already closed or completed. You cannot log new data against it.")

            allocation, created = MaterialAllocation.objects.get_or_create(
                job_order=jo,
                material=live_material,
                defaults={
                    'required_kg': Decimal('0'), 'allocated_kg': Decimal('0'), 
                    'shortfall_kg': Decimal('0'), 'actual_used_kg': Decimal('0')
                }
            )

            is_sub = False
            if created or allocation.required_kg == Decimal('0'):
                is_sub = True
                live_material.current_stock_kg -= amount_kg
            else:
                # Deduct from warehouse stock ONLY if usage exceeds the previously allocated amount
                if (allocation.actual_used_kg + amount_kg) > allocation.allocated_kg:
                    overage = min(amount_kg, (allocation.actual_used_kg + amount_kg) - allocation.allocated_kg)
                    if overage > Decimal('0'):
                        live_material.current_stock_kg -= overage

            live_material.save()
            allocation.actual_used_kg += amount_kg
            allocation.save()

            # Create the log cleanly without triggering recursive save logic
            log = cls.objects.create(
                job_order=jo,
                material=live_material,
                amount_kg=amount_kg,
                operator_name=operator_name,
                is_substitution=is_sub
            )
            
            return log, allocation.is_overused
        
# -----------------------------------------------------------------------------
# FLOOR PRODUCTION LOGS (STATEFUL SESSIONS)
# -----------------------------------------------------------------------------

class ExtrusionSession(models.Model):
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='extrusion_sessions')
    machine_no = models.CharField(max_length=10)
    shift = models.CharField(max_length=10, choices=[('AM', 'Morning'), ('PM', 'Night')])
    operator_name = models.CharField(max_length=50, null=True, blank=True)
    
    target_amount_kg = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default='ACTIVE', choices=[('ACTIVE', 'Active'), ('COMPLETED', 'Completed')])
    
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)
    
    total_output_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_wastage_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    # --- NEW ACCOUNTABILITY METRICS ---
    returned_material_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    final_wastage_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    unaccounted_variance_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    transferred_to_next_job_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    
    version = models.PositiveIntegerField(default=1, help_text="Increments on every update to prevent multi-tab collision.")

    def stop_session(self):
        if self.status == 'ACTIVE':
            self.status = 'COMPLETED'
            self.end_time = timezone.now()
            
            # We add the final machine purge to the total wastage for the Job Order
            total_session_waste = self.total_wastage_kg + self.final_wastage_kg
            
            # Cascade totals to the JobOrder
            JobOrder.objects.filter(pk=self.job_order.pk).update(
                total_extruded_kg=F('total_extruded_kg') + self.total_output_kg,
                total_extrusion_wastage_kg=F('total_extrusion_wastage_kg') + total_session_waste
            )
            self.save()
    
    # Add this method inside the ExtrusionSession class in models.py

    @transaction.atomic
    def purge_and_close(self, returned_kg, final_waste, force_discrepancy=False):
        """
        Protocol A: The Strict Reconciliation Gateway.
        Safely reconciles material usage, flags discrepancies, and returns unused stock.
        """
        if self.status != 'ACTIVE':
            raise ValidationError("Cannot close a session that is no longer active.")

        total_reserved = sum(sm.reserved_kg for sm in self.materials.all())
        total_consumed = self.total_output_kg + self.total_wastage_kg
        
        accounted_for = total_consumed + returned_kg + final_waste
        variance = total_reserved - accounted_for
        
        buffer = total_reserved * Decimal('0.01')
        
        if abs(variance) > buffer and not force_discrepancy:
            raise ValidationError(f"Discrepancy detected! You are missing {variance:.2f}kg of material. Please recount or flag a discrepancy.")
            
        self.returned_material_kg = returned_kg
        self.final_wastage_kg = final_waste
        
        if force_discrepancy:
            self.unaccounted_variance_kg = variance
            
        # BUG FIX: Ensure the warehouse material is locked before refunding stock
        if returned_kg > Decimal('0'):
            primary_material = self.materials.first()
            if primary_material:
                raw_mat = RawMaterial.objects.select_for_update().get(pk=primary_material.material.pk)
                raw_mat.current_stock_kg += returned_kg
                raw_mat.save()
                
        self.stop_session()

    def __str__(self):
        return f"{self.machine_no} | {self.job_order.jo_number} ({self.status})"

class SessionMaterial(models.Model):
    session = models.ForeignKey(ExtrusionSession, on_delete=models.CASCADE, related_name='materials')
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE)
    reserved_kg = models.DecimalField(max_digits=10, decimal_places=2)
    actual_used_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

class ExtrusionLog(models.Model):
    session = models.ForeignKey(ExtrusionSession, on_delete=models.CASCADE, related_name='rolls')
    timestamp = models.DateTimeField(auto_now_add=True)
    
    roll_weight_kg = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    wastage_kg = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))])

    def clean(self):
        if self.wastage_kg < Decimal('0') or self.wastage_kg > self.roll_weight_kg:
            raise ValidationError({'wastage_kg': 'Wastage cannot be negative or greater than the total roll weight.'})
        
        # --- NEW LOGIC: Conservation of Mass ---
        if self.session_id:
            # 1. Total material physically loaded into the hopper for this session
            total_reserved = sum(sm.reserved_kg for sm in self.session.materials.all())
            
            # 2. Material already converted into past rolls or past wastage
            already_consumed = self.session.total_output_kg + self.session.total_wastage_kg
            
            # 3. What is physically left? (Use max to prevent negative comparisons)
            remaining_material = max(Decimal('0'), total_reserved - already_consumed)
            
            # 4. What is the operator claiming they just produced?
            attempted_consumption = self.roll_weight_kg + self.wastage_kg

            # We apply a 2% buffer just like Cutting/Packing to forgive slight scale miscalibrations.
            # If they exceed this, block the log entirely.
            if self.pk is None and attempted_consumption > (remaining_material * Decimal('1.02')):
                raise ValidationError({
                    'roll_weight_kg': f'Physical limit exceeded: Attempting to log {attempted_consumption:.1f}kg (Roll + Wastage), but only {remaining_material:.1f}kg of reserved material remains.'
                })

    def save(self, *args, **kwargs):
        self.clean()
        is_new = self.pk is None 
        super().save(*args, **kwargs)

        if is_new:
            session = self.session
            
            # REFACTOR: Increment the session version atomically alongside the totals
            ExtrusionSession.objects.filter(pk=session.pk).update(
                total_output_kg=F('total_output_kg') + self.roll_weight_kg,
                total_wastage_kg=F('total_wastage_kg') + self.wastage_kg,
                version=F('version') + 1  # OCC Increment
            )

            JobOrder.objects.filter(pk=session.job_order.pk).update(
                total_extruded_kg=F('total_extruded_kg') + self.roll_weight_kg
            )
            
            session.refresh_from_db()
            
            # --- NEW AUTO-COMPLETE LOGIC ---
            total_reserved = sum(sm.reserved_kg for sm in session.materials.all())
            total_consumed = session.total_output_kg + session.total_wastage_kg
            
            if session.total_output_kg >= session.target_amount_kg:
                session.stop_session()
            elif total_consumed >= total_reserved and total_reserved > Decimal('0'):
                session.stop_session()

    @classmethod
    def get_total_output(cls, date_filter):
        return cls.objects.filter(**date_filter).aggregate(
            total=Coalesce(Sum('roll_weight_kg'), Decimal('0.00'), output_field=DecimalField())
        )['total']

    @classmethod
    def get_macro_breakdown(cls, date_filter):
        return cls.objects.filter(**date_filter).values(
            jo_num=F('session__job_order__jo_number'),
            customer=F('session__job_order__customer')
        ).annotate(total=Sum('roll_weight_kg')).order_by('-total')

class CuttingSession(models.Model):
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='cutting_sessions')
    machine_no = models.CharField(max_length=10)
    shift = models.CharField(max_length=10, choices=[('AM', 'Morning'), ('PM', 'Night')])
    operator_name = models.CharField(max_length=50, null=True, blank=True)
    
    input_roll_weight_kg = models.DecimalField(max_digits=10, decimal_places=2, help_text="Weight of the roll loaded onto the machine")
    
    status = models.CharField(max_length=20, default='ACTIVE', choices=[('ACTIVE', 'Active'), ('COMPLETED', 'Completed'), ('ENDED_EARLY', 'Ended Early')])
    
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)
    
    total_output_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_wastage_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    version = models.PositiveIntegerField(default=1)

    def stop_session(self, calculate_wastage=True):
        """Terminates the session and handles wastage calculation."""
        if self.status != 'ACTIVE':
            return
            
        with transaction.atomic():
            self.end_time = timezone.now()
            
            if calculate_wastage:
                wastage = self.input_roll_weight_kg - self.total_output_kg
                if wastage > Decimal('0'):
                    self.total_wastage_kg = wastage
                    
                    # Cascade the calculated wastage up to the Job Order
                    JobOrder.objects.filter(pk=self.job_order.pk).update(
                        total_cutting_wastage_kg=F('total_cutting_wastage_kg') + self.total_wastage_kg
                    )
                self.status = 'COMPLETED'
            else:
                self.status = 'ENDED_EARLY'
                
            self.save(update_fields=['status', 'end_time', 'total_wastage_kg'])

    def __str__(self):
        return f"Cut Machine {self.machine_no} | JO: {self.job_order.jo_number} ({self.status})"


class CuttingLog(models.Model):
    session = models.ForeignKey(CuttingSession, on_delete=models.CASCADE, related_name='logs', null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    
    # We no longer need machine, shift, operator, or wastage_kg here as the Session handles it!
    output_kg = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])

    def clean(self):
        if self.session_id:
            remaining = self.session.input_roll_weight_kg - self.session.total_output_kg
            # Allow a tiny 5% buffer for scale miscalibration
            if self.pk is None and self.output_kg > (remaining * Decimal('1.05')):
                raise ValidationError({'output_kg': f'Cannot log {self.output_kg}kg. Only {remaining:.1f}kg remains on this roll.'})

    def save(self, *args, **kwargs):
        self.clean()
        is_new = self.pk is None
        super().save(*args, **kwargs)
        
        if is_new:
            session = self.session
            
            # REFACTOR: Increment the cutting session version atomically
            CuttingSession.objects.filter(pk=session.pk).update(
                total_output_kg=F('total_output_kg') + self.output_kg,
                version=F('version') + 1  # OCC Increment
            )
            
            JobOrder.objects.filter(pk=session.job_order.pk).update(
                total_cut_kg=F('total_cut_kg') + self.output_kg
            )
            
            session.refresh_from_db()
            if session.total_output_kg >= session.input_roll_weight_kg:
                session.stop_session(calculate_wastage=True)

    @classmethod
    def get_total_output(cls, date_filter):
        return cls.objects.filter(**date_filter).aggregate(
            total=Coalesce(Sum('output_kg'), Decimal('0.00'), output_field=DecimalField())
        )['total']

    @classmethod
    def get_macro_breakdown(cls, date_filter):
        return cls.objects.filter(**date_filter).values(
            jo_num=F('session__job_order__jo_number'),
            customer=F('session__job_order__customer')
        ).annotate(total=Sum('output_kg')).order_by('-total')

class PackingLog(models.Model):
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='packing_logs')
    timestamp = models.DateTimeField(auto_now_add=True)
    operator_name = models.CharField(max_length=50, null=True, blank=True)
    
    packing_size_kg = models.DecimalField(max_digits=6, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))], help_text="KG per Bag/Pallet")
    quantity_packed = models.IntegerField(validators=[MinValueValidator(1)], help_text="Number of Bags/Pallets")

    def clean(self):
        if self.job_order_id:
            remaining = self.job_order.total_cut_kg - self.job_order.total_packed_kg
            total_weight = self.packing_size_kg * Decimal(str(self.quantity_packed))
            if self.pk is None and total_weight > (remaining * Decimal('1.05')):
                raise ValidationError({'quantity_packed': f'Attempting to pack {total_weight:.1f}kg, but only {remaining:.1f}kg has been cut and is available.'})

    def save(self, *args, **kwargs):
        self.clean()
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            total_weight = self.packing_size_kg * Decimal(str(self.quantity_packed))
            JobOrder.objects.filter(pk=self.job_order.pk).update(
                total_packed_kg=F('total_packed_kg') + total_weight
            )

    @classmethod
    def get_total_output(cls, date_filter):
        return cls.objects.filter(**date_filter).annotate(
            weight=F('packing_size_kg') * F('quantity_packed')
        ).aggregate(
            total=Coalesce(Sum('weight'), Decimal('0.00'), output_field=DecimalField())
        )['total']

    @classmethod
    def get_macro_breakdown(cls, date_filter):
        return cls.objects.filter(**date_filter).values(
            jo_num=F('job_order__jo_number'),
            customer=F('job_order__customer')
        ).annotate(total=Sum(F('packing_size_kg') * F('quantity_packed'))).order_by('-total')

class DispatchLog(models.Model):
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='shipments')
    dispatch_date = models.DateTimeField(auto_now_add=True)
    shipped_kg = models.DecimalField(max_digits=10, decimal_places=2)
    delivery_order_no = models.CharField(max_length=50, blank=True)

    def clean(self):
        if self.shipped_kg <= Decimal('0'):
            raise ValidationError({'shipped_kg': 'Shipped quantity must be greater than zero.'})
            
        if self.job_order_id:
            remaining = self.job_order.total_packed_kg - self.job_order.total_shipped_kg
            if self.pk is None and self.shipped_kg > remaining:
                raise ValidationError({'shipped_kg': f'Cannot dispatch {self.shipped_kg}kg. Only {remaining:.1f}kg is packed and ready for shipping.'})

    def save(self, *args, **kwargs):
        self.clean()
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            JobOrder.objects.filter(pk=self.job_order.pk).update(
                total_shipped_kg=F('total_shipped_kg') + self.shipped_kg
            )