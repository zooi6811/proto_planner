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
    ACTION_CHOICES = [
        ('SESSION_STARTED', 'Session Started'),
        ('SESSION_STOPPED', 'Session Stopped'),
        ('SESSION_PURGED', 'Session Purged & Reconciled'),
        ('MATERIAL_USED', 'Material Used / Deducted'),
        ('OUTPUT_LOGGED', 'Production Output Logged'),
        ('JOB_COMPLETED', 'Job Fully Completed'),
        ('YIELD_ADAPTATION', 'Recipe Yield Adapted'),
    ]
    
    operator_name = models.CharField(max_length=50, help_text="User or operator who performed the action")
    action_type = models.CharField(max_length=50, choices=ACTION_CHOICES)
    
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey('content_type', 'object_id')
    
    details = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['content_type', 'object_id']),
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
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE, related_name='restocks')
    arrival_date = models.DateTimeField(auto_now_add=True)
    amount_kg = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    
    supplier = models.CharField(max_length=100, blank=True, default="-")
    po_number = models.CharField(max_length=50, blank=True, default="-")
    recorded_by = models.CharField(max_length=50, default="Admin")

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        
        if is_new:
            with transaction.atomic():
                mat = RawMaterial.objects.select_for_update().get(pk=self.material.pk)
                incoming_stock = self.amount_kg
                
                # --- FIX #5: RETROACTIVE SHORTFALL FULFILMENT ---
                # Fetch all active allocations waiting for this material, strictly ordered by priority
                shortfall_allocations = MaterialAllocation.objects.filter(
                    material=mat,
                    shortfall_kg__gt=0,
                    job_order__is_completed=False
                ).select_related('job_order').order_by(
                    'job_order__queue_position', 
                    'job_order__target_delivery_date',
                    'job_order__id'
                ).select_for_update()
                
                for allocation in shortfall_allocations:
                    if incoming_stock <= Decimal('0'):
                        break # The delivery truck is empty; stop fulfilling
                        
                    # Calculate how much of this specific shortfall we can clear
                    fulfillable_amount = min(incoming_stock, allocation.shortfall_kg)
                    
                    # Move the stock directly from the delivery into the Job's Escrow
                    allocation.allocated_kg += fulfillable_amount
                    allocation.shortfall_kg -= fulfillable_amount
                    allocation.save(update_fields=['allocated_kg', 'shortfall_kg'])
                    
                    # Deduct from our running total of incoming stock
                    incoming_stock -= fulfillable_amount
                    
                # Add whatever is left over (if any) to the global warehouse shelves
                mat.current_stock_kg += incoming_stock
                mat.save(update_fields=['current_stock_kg'])

    def __str__(self):
        return f"+{self.amount_kg} KG of {self.material.name} on {self.arrival_date.strftime('%Y-%m-%d')}"

class Recipe(models.Model):
    formula_code = models.CharField(max_length=50, unique=True)
    description = models.CharField(max_length=200, blank=True)
    
    # Predictive yield planning targets
    cutting_wastage_rate = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal('0.05'))
    extrusion_wastage_rate = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal('0.05'))
    
    # NEW (Part 4): Bootstrap tracking to weigh early data safely
    extrusion_session_count = models.PositiveIntegerField(default=0)
    cutting_session_count = models.PositiveIntegerField(default=0)
    
    def __str__(self):
        return self.formula_code

    @classmethod
    @transaction.atomic
    def adapt_wastage_rate(cls, recipe_id, stage, observed_yield, session, operator_name):
        """
        Self-correcting feedback loop using Exponential Moving Average (EMA).
        Strictly operates on the planning layer without mutating physical stock.
        """
        if not recipe_id: 
            return
            
        # Part 4 Validation: Reject physically impossible yields
        if observed_yield <= Decimal('0') or observed_yield > Decimal('1.0000'):
            return
            
        observed_wastage = Decimal('1.0000') - observed_yield
        
        # Part 4 Validation: Clamp extreme anomalies to prevent catastrophic skewing (cap at 50%)
        observed_wastage = max(Decimal('0.0000'), min(Decimal('0.5000'), observed_wastage))

        # Part 3 Constraint: Lock the recipe row to prevent race conditions during concurrent closures
        recipe = cls.objects.select_for_update().get(pk=recipe_id)
        
        if stage == 'EXTRUSION':
            recipe.extrusion_session_count += 1
            history_count = recipe.extrusion_session_count
            old_wastage = recipe.extrusion_wastage_rate
        else:
            recipe.cutting_session_count += 1
            history_count = recipe.cutting_session_count
            old_wastage = recipe.cutting_wastage_rate
            
        # Part 4 Validation: Handle small sample sizes (Bootstrap Phase)
        # If the recipe is brand new (under 5 sessions), we restrict the EMA weighting
        # to prevent a single bad initial run from completely ruining the predictive model.
        if history_count < 5:
            alpha = Decimal('0.05')
        else:
            alpha = Decimal('0.20')
            
        new_wastage = (alpha * observed_wastage) + ((Decimal('1.0000') - alpha) * old_wastage)
        
        if stage == 'EXTRUSION':
            recipe.extrusion_wastage_rate = new_wastage
        else:
            recipe.cutting_wastage_rate = new_wastage
            
        recipe.save(update_fields=[
            'extrusion_wastage_rate', 'cutting_wastage_rate', 
            'extrusion_session_count', 'cutting_session_count'
        ])
        
        # Part 3 Constraint: Ensure comprehensive audit trailing with all observed metrics
        AuditLog.objects.create(
            operator_name=operator_name,
            action_type='YIELD_ADAPTATION',
            content_object=recipe,
            details={
                'stage': stage,
                'session_id': session.pk,
                'observed_yield': str(round(observed_yield, 4)),
                'observed_wastage_rate': str(round(observed_wastage, 4)),
                'previous_predictive_wastage': str(round(old_wastage, 4)),
                'new_predictive_wastage': str(round(new_wastage, 4)),
                'alpha_used': str(alpha),
                'total_sessions_analysed': history_count
            }
        )

        from .signals import yield_adapted 
        
        # Fire the signal to update the Control Tower
        yield_adapted.send(
            sender=cls,
            recipe_id=recipe_id,
            stage=stage,
            new_wastage=new_wastage
        )

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


class JobOrderManager(Manager):
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
        help_text="Lower numbers run first. Use 100 for standard/un-queued jobs."
    )
    
    po_number = models.CharField(max_length=50, blank=True, default="-")
    target_delivery_date = models.DateField(null=True, blank=True)
    product_dimension = models.CharField(max_length=100, default="")
    recipe = models.ForeignKey(Recipe, on_delete=models.SET_NULL, null=True, blank=True)
    
    printing_required = models.BooleanField(default=False)
    sealing_required = models.BooleanField(default=False)
    slitting_required = models.BooleanField(default=False)
    remarks = models.TextField(blank=True, default="-")
    
    wastage_buffer_percent = models.DecimalField(max_digits=5, decimal_places=2, default=10.00)
    order_quantity_kg = models.DecimalField(max_digits=10, decimal_places=2)
    total_extrusion_wastage_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    
    total_est_material_kg = models.DecimalField(max_digits=10, decimal_places=2, editable=False, default=Decimal('0.00'))
    
    estimated_extrusion_target_kg = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), editable=False)
    estimated_material_required_kg = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), editable=False)

    total_cutting_wastage_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_extruded_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_cut_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_packed_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_shipped_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    is_completed = models.BooleanField(default=False)

    objects = JobOrderManager()

    class Meta:
        ordering = ['is_completed', 'queue_position', 'target_delivery_date']

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        
        # If it's a brand new job and has a recipe, we lock the recipe row 
        # to ensure we don't read a yield value that is mid-update.
        if is_new and getattr(self, 'recipe_id', None):
            with transaction.atomic():
                # Lock the recipe strictly for this transaction
                live_recipe = Recipe.objects.select_for_update().get(pk=self.recipe_id)
                
                if self.order_quantity_kg:
                    cut_waste_rate = live_recipe.cutting_wastage_rate
                    ext_waste_rate = live_recipe.extrusion_wastage_rate
                    
                    if cut_waste_rate >= Decimal('1.0'): cut_waste_rate = Decimal('0.99')
                    if ext_waste_rate >= Decimal('1.0'): ext_waste_rate = Decimal('0.99')

                    cutting_yield = Decimal('1.0000') - cut_waste_rate
                    extrusion_yield = Decimal('1.0000') - ext_waste_rate

                    self.estimated_extrusion_target_kg = self.order_quantity_kg / cutting_yield
                    self.estimated_material_required_kg = self.order_quantity_kg / (cutting_yield * extrusion_yield)
                    
                    self.total_est_material_kg = self.estimated_material_required_kg
                
                # Save the JobOrder to get a Primary Key before allocating materials
                super().save(*args, **kwargs)
                
                # Upfront Material Allocation
                for recipe_item in live_recipe.ingredients.all():
                    material = RawMaterial.objects.select_for_update().get(pk=recipe_item.material.pk)
                    required_amount = self.estimated_material_required_kg * recipe_item.ratio
                    
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
        else:
            # Standard save behaviour for updates to existing jobs
            if self.order_quantity_kg and not getattr(self, 'estimated_material_required_kg', None):
                # Fallback for updating legacy records missing the new fields
                self.estimated_material_required_kg = self.order_quantity_kg * Decimal('1.10')
                self.estimated_extrusion_target_kg = self.order_quantity_kg * Decimal('1.05')
                self.total_est_material_kg = self.estimated_material_required_kg
                
            super().save(*args, **kwargs)

    def complete_job(self, operator_name="System", force_close=False):
        """
        Safely closes the job, handles material reconciliation, accommodates 
        manufacturing variances, and enforces Mass Balance data integrity.
        """
        with transaction.atomic():
            job = JobOrder.objects.select_for_update().get(pk=self.pk)
            
            if job.is_completed:
                return False, "Job is already marked as completed."

            # --- 1. THE MASS BALANCE CHECK (Catches Typos & Missing Data) ---
            # Are we missing more than 2% of the physical material?
            if job.mass_discrepancy_percent > Decimal('2.00') and not force_close:
                return False, (
                    f"DATA ERROR: Mass balance failed. Missing/Extra {abs(job.mass_discrepancy_kg)}kg "
                    f"({job.mass_discrepancy_percent:.1f}% discrepancy). Please check for typos in roll weights "
                    f"or unlogged machine wastage before closing."
                )

            # --- 2. THE PRODUCTION TOLERANCE CHECK (Catches Over-runs) ---
            # Did we overproduce or underproduce by more than 10%?
            variance_kg = job.total_packed_kg - job.order_quantity_kg
            variance_pct = (abs(variance_kg) / job.order_quantity_kg) * Decimal('100') if job.order_quantity_kg else Decimal('0')
            
            if variance_pct > Decimal('10.00') and not force_close:
                state = "OVERPRODUCED" if variance_kg > 0 else "UNDERPRODUCED"
                alert_msg = (
                    f"VARIANCE ALERT: Job {state} by {abs(variance_kg)}kg ({variance_pct:.1f}%). "
                    f"Supervisor override (force_close) is required to approve this extreme variance."
                )
            
                from .signals import trigger_supervisor_alert # Lazy import
                trigger_supervisor_alert(job.jo_number, state, alert_msg)
                
                return False, alert_msg

            # If not forcing a premature closure, wait for shipments to meet the packed target
            # (Note: We check against packed_kg now to ensure we ship what we actually packed)
            if not force_close and job.total_shipped_kg < job.total_packed_kg:
                return False, "Job has not shipped all packed goods yet."

            # Accommodate legitimate overproduction upscaling
            if job.total_packed_kg > job.order_quantity_kg:
                job.order_quantity_kg = job.total_packed_kg

            # RECONCILIATION: Return unused allocated materials to warehouse stock
            for allocation in job.allocations.select_for_update().all():
                unused_kg = allocation.allocated_kg - allocation.actual_used_kg
                if unused_kg > Decimal('0.00'):
                    material = RawMaterial.objects.select_for_update().get(pk=allocation.material.pk)
                    material.current_stock_kg += unused_kg
                    material.save(update_fields=['current_stock_kg'])
                    
                    allocation.allocated_kg = allocation.actual_used_kg
                    allocation.save(update_fields=['allocated_kg'])
            
            # Finalise lockdown state
            job.is_completed = True
            job.save(update_fields=['is_completed', 'order_quantity_kg'])
            
            self.is_completed = True
            self.order_quantity_kg = job.order_quantity_kg
            
            # Audit Trail including Data Integrity metrics
            from .models import AuditLog 
            AuditLog.objects.create(
                operator_name=operator_name,
                action_type='JOB_COMPLETED',
                content_object=job,
                details={
                    'final_order_quantity_kg': str(job.order_quantity_kg),
                    'total_extruded_kg': str(job.total_extruded_kg),
                    'total_cut_kg': str(job.total_cut_kg),
                    'total_packed_kg': str(job.total_packed_kg),
                    'mass_discrepancy_percent': str(round(job.mass_discrepancy_percent, 2)),
                    'forced_closure': force_close,
                    'reconciliation_performed': True
                }
            )
            
            return True, "Job successfully completed and materials reconciled."
        
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
        total_material_processed = self.total_extruded_kg + self.total_extrusion_wastage_kg
        if total_material_processed > Decimal('0.00'):
            return round((self.total_extrusion_wastage_kg / total_material_processed) * Decimal('100'), 2)
        return Decimal('0.00')

    @property
    def cutting_wastage_pct(self):
        total_material_processed = self.total_cut_kg + self.total_cutting_wastage_kg
        if total_material_processed > Decimal('0.00'):
            return round((self.total_cutting_wastage_kg / total_material_processed) * Decimal('100'), 2)
        return Decimal('0.00')

    @property
    def overall_wastage_pct(self):
        total_waste = self.total_extrusion_wastage_kg + self.total_cutting_wastage_kg
        total_input = self.total_extruded_kg + self.total_extrusion_wastage_kg 
        
        if total_input > Decimal('0.00'):
            return round((total_waste / total_input) * Decimal('100'), 2)
        return Decimal('0.00')
    
    @property
    def total_material_consumed_kg(self):
        # Total actual raw material pulled from the warehouse for this job
        return sum(alloc.actual_used_kg for alloc in self.allocations.all())

    @property
    def total_accounted_mass_kg(self):
        # Finished goods + all recorded scrap
        return self.total_packed_kg + self.total_extrusion_wastage_kg + self.total_cutting_wastage_kg

    @property
    def mass_discrepancy_kg(self):
        # The amount of physical material floating in the void
        return self.total_material_consumed_kg - self.total_accounted_mass_kg

    @property
    def mass_discrepancy_percent(self):
        if self.total_material_consumed_kg > Decimal('0'):
            return abs(self.mass_discrepancy_kg / self.total_material_consumed_kg) * Decimal('100')
        return Decimal('0.00')

    def __str__(self):
        return f"JO: {self.jo_number} - {self.customer}"


class MaterialAllocation(models.Model):
    job_order = models.ForeignKey(JobOrder, on_delete=models.CASCADE, related_name='allocations')
    material = models.ForeignKey(RawMaterial, on_delete=models.CASCADE)
    
    required_kg = models.DecimalField(max_digits=10, decimal_places=2)
    allocated_kg = models.DecimalField(max_digits=10, decimal_places=2)
    shortfall_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
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

    @classmethod
    def record_usage(cls, job_order, material, amount_kg, operator_name):
        """
        Service method to handle ad-hoc material usage safely.
        Validates stock, prevents session clashes, and handles allocation logic within a strict atomic lock.
        """
        with transaction.atomic():
            jo = JobOrder.objects.select_for_update().get(pk=job_order.pk)
            
            if jo.is_completed:
                raise ValidationError("This Job Order is already closed or completed. You cannot log new data against it.")
                
            # --- FIX #4: PREVENT DOUBLE-COUNTING CLASHES ---
            # If the machine is running, force them to use the Session hopper instead of the ad-hoc form
            if jo.extrusion_sessions.filter(status='ACTIVE').exists():
                raise ValidationError(
                    "Cannot log manual ad-hoc usage while an Extrusion machine is actively running this job. "
                    "Please load materials directly into the machine session's hopper instead to prevent double-counting."
                )

            live_material = RawMaterial.objects.select_for_update().get(pk=material.pk)

            if amount_kg > live_material.current_stock_kg:
                raise ValidationError(f"Insufficient stock. You requested {amount_kg}kg, but only {live_material.current_stock_kg}kg of {live_material.name} is available.")

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
                if (allocation.actual_used_kg + amount_kg) > allocation.allocated_kg:
                    overage = min(amount_kg, (allocation.actual_used_kg + amount_kg) - allocation.allocated_kg)
                    if overage > Decimal('0'):
                        live_material.current_stock_kg -= overage

            live_material.save(update_fields=['current_stock_kg'])
            allocation.actual_used_kg += amount_kg
            allocation.save(update_fields=['actual_used_kg'])

            log = cls.objects.create(
                job_order=jo,
                material=live_material,
                amount_kg=amount_kg,
                operator_name=operator_name,
                is_substitution=is_sub
            )
            
            return log, allocation.is_overused


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

    returned_material_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    final_wastage_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    unaccounted_variance_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    transferred_to_next_job_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    
    version = models.PositiveIntegerField(default=1)

    @classmethod
    @transaction.atomic
    def start_session(cls, job_order, machine_no, shift, target_amount, operator, material_reservations):
        """Service method to initiate a new extrusion session using Escrowed materials and strict Recipe Adherence."""
        if cls.objects.select_for_update().filter(machine_no=machine_no, status='ACTIVE').exists():
            raise ValidationError(f"Conflict: Machine {machine_no} is already running an active session.")

        session = cls.objects.create(
            job_order=job_order, machine_no=machine_no, shift=shift, 
            target_amount_kg=target_amount, operator_name=operator
        )
        
        total_reserved = sum(amt for mat_id, amt in material_reservations if amt > Decimal('0'))
        
        # --- NEW: RECIPE ADHERENCE CHECK (BOM Validation) ---
        if job_order.recipe and total_reserved > Decimal('0'):
            # Create a dictionary of the allowed materials and their target ratios
            recipe_items = {item.material_id: item.ratio for item in job_order.recipe.ingredients.all()}
            
            for mat_id, parsed_amount in material_reservations:
                if parsed_amount > Decimal('0'):
                    mat_pk = int(mat_id)
                    
                    # 1. Unauthorised Material Check
                    if mat_pk not in recipe_items:
                        bad_mat = RawMaterial.objects.get(id=mat_pk)
                        raise ValidationError(f"Recipe Deviation: '{bad_mat.name}' is not authorised for formula {job_order.recipe.formula_code}.")
                    
                    # 2. Ratio Tolerance Check
                    actual_ratio = parsed_amount / total_reserved
                    expected_ratio = recipe_items[mat_pk]
                    
                    # Enforce a strict 3% tolerance for manual weighing variances
                    if abs(actual_ratio - expected_ratio) > Decimal('0.03'):
                        bad_mat = RawMaterial.objects.get(id=mat_pk)
                        raise ValidationError(
                            f"Recipe Deviation: '{bad_mat.name}' makes up {actual_ratio * Decimal('100'):.1f}% of the hopper mix, "
                            f"but the recipe strictly requires {expected_ratio * Decimal('100'):.1f}%. "
                            f"Please adjust your physical hopper weights before starting the machine."
                        )
        # ----------------------------------------------------

        # (The Escrow Logic we built in Step 1 continues here...)
        for mat_id, parsed_amount in material_reservations:
            if mat_id and parsed_amount > Decimal('0'):
                mat = RawMaterial.objects.select_for_update().get(id=mat_id)
                
                allocation, created = MaterialAllocation.objects.get_or_create(
                    job_order=job_order,
                    material=mat,
                    defaults={
                        'required_kg': Decimal('0'), 'allocated_kg': Decimal('0'), 
                        'shortfall_kg': Decimal('0'), 'actual_used_kg': Decimal('0')
                    }
                )
                
                unconsumed_allocation = allocation.allocated_kg - allocation.actual_used_kg
                
                if parsed_amount > unconsumed_allocation:
                    overage = parsed_amount - unconsumed_allocation
                    
                    if overage > mat.current_stock_kg:
                        raise ValidationError(
                            f"Insufficient stock. You need {parsed_amount}kg of {mat.name}, "
                            f"but only {unconsumed_allocation:.2f}kg remains in the job allocation, "
                            f"and the warehouse only has {mat.current_stock_kg:.2f}kg available."
                        )
                    
                    mat.current_stock_kg -= overage
                    mat.save(update_fields=['current_stock_kg'])
                    
                    allocation.allocated_kg += overage
                    allocation.save(update_fields=['allocated_kg'])
                
                SessionMaterial.objects.create(
                    session=session, material=mat, reserved_kg=parsed_amount
                )
        
        AuditLog.objects.create(
            operator_name=operator,
            action_type='SESSION_STARTED',
            content_object=session,
            details={
                'machine_no': machine_no,
                'job_order': job_order.jo_number,
                'target_amount_kg': str(target_amount),
                'total_reserved_kg': str(total_reserved)
            }
        )
        return session

    def stop_session(self):
        if self.status == 'ACTIVE':
            self.status = 'COMPLETED'
            self.end_time = timezone.now()
            
            # Add the wastage to the Job Order
            total_session_waste = self.total_wastage_kg + getattr(self, 'final_wastage_kg', Decimal('0.00'))
            
            JobOrder.objects.filter(pk=self.job_order.pk).update(
                total_extrusion_wastage_kg=F('total_extrusion_wastage_kg') + total_session_waste
            )
            
            # --- FIX #3: Proportional 'actual_used_kg' Deduction ---
            deductions = getattr(self, 'returned_material_kg', Decimal('0')) + getattr(self, 'transferred_to_next_job_kg', Decimal('0'))
            total_reserved = sum(sm.reserved_kg for sm in self.materials.all())
            
            # Order by material_id to strictly prevent database deadlocks during select_for_update
            for sm in self.materials.order_by('material_id'):
                used = sm.reserved_kg
                
                # If there are deductions and the hopper wasn't empty
                if deductions > Decimal('0') and total_reserved > Decimal('0'):
                    # Calculate this specific material's % share of the total hopper mix
                    proportion = sm.reserved_kg / total_reserved
                    
                    # Deduct its exact proportional share of the returned/purged material
                    material_deduction = deductions * proportion
                    used -= material_deduction
                    
                    # Safety clamp just in case of microscopic floating-point anomalies
                    if used < Decimal('0'):
                        used = Decimal('0')
                        
                sm.actual_used_kg = used
                sm.save(update_fields=['actual_used_kg'])
                
                # Push the final consumed amount up to the Job's Allocation Escrow
                allocation = MaterialAllocation.objects.select_for_update().get(
                    job_order=self.job_order, material=sm.material
                )
                allocation.actual_used_kg += sm.actual_used_kg
                allocation.save(update_fields=['actual_used_kg'])
            # --------------------------------------------------------------

            self.save()
            
            # --- YIELD ADAPTATION ---
            total_consumed = self.total_output_kg + total_session_waste
            if total_consumed > Decimal('0') and self.job_order.recipe_id:
                actual_yield = self.total_output_kg / total_consumed
                operator = self.operator_name or "System"
                
                from .models import Recipe 
                Recipe.adapt_wastage_rate(
                    recipe_id=self.job_order.recipe_id,
                    stage='EXTRUSION',
                    observed_yield=actual_yield,
                    session=self,
                    operator_name=operator
                )
            
    @transaction.atomic
    def terminate_early(self, operator_name):
        self.stop_session()
        AuditLog.objects.create(
            operator_name=operator_name,
            action_type='SESSION_STOPPED',
            content_object=self,
            details={
                'machine_no': self.machine_no,
                'job_order': self.job_order.jo_number,
                'reason': 'Manual early termination',
                'total_output_kg_at_stop': str(self.total_output_kg)
            }
        )

    @transaction.atomic
    def handover_shift(self, new_operator, new_shift):
        self.operator_name = new_operator
        self.shift = new_shift
        self.save(update_fields=['operator_name', 'shift'])

    @transaction.atomic
    def rollover_to_job(self, next_job):
        total_reserved = sum(sm.reserved_kg for sm in self.materials.all())
        total_consumed = self.total_output_kg + self.total_wastage_kg
        remaining_balance = max(Decimal('0.00'), total_reserved - total_consumed)
        
        new_session = ExtrusionSession.objects.create(
            machine_no=self.machine_no,
            job_order=next_job,
            operator_name=self.operator_name,
            shift=self.shift,
            status='ACTIVE',
            target_amount_kg=next_job.remaining_extrusion_kg 
        )
        
        primary_material_record = self.materials.first()
        if primary_material_record and remaining_balance > 0:
            SessionMaterial.objects.create(
                session=new_session,
                material=primary_material_record.material,
                reserved_kg=remaining_balance
            )
            
        self.transferred_to_next_job_kg = remaining_balance
        self.stop_session()
        return new_session

    @transaction.atomic
    def purge_and_close(self, returned_kg, final_waste, force_discrepancy=False):
        """
        Protocol A: The Strict Reconciliation Gateway.
        Safely reconciles material usage, flags discrepancies, and stops the session.
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
            
        # --- FIX: DIRECT WAREHOUSE REFUND REMOVED ---
        # We no longer manually add returned_kg back to RawMaterial here.
        # stop_session() handles the math so the returned amount stays in the Job's Escrow.
        # When the JobOrder is closed, all unconsumed Escrow is cleanly refunded at once.
                
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

    @classmethod
    def record_log(cls, session, roll_weight, wastage, submitted_version, operator):
        with transaction.atomic():
            session = ExtrusionSession.objects.select_for_update().get(id=session.id)
            if session.status != 'ACTIVE':
                raise ValidationError("This machine session is no longer active. Please start a new run.")
                
            if submitted_version and int(submitted_version) != session.version:
                raise ValidationError("Stale Data Error: This session was updated in another tab or by another operator. Please refresh the machine state.")
                
            log = cls.objects.create(session=session, roll_weight_kg=roll_weight, wastage_kg=wastage)
            
            AuditLog.objects.create(
                operator_name=operator,
                action_type='OUTPUT_LOGGED',
                content_object=session,
                details={
                    'machine_no': session.machine_no,
                    'job_order': session.job_order.jo_number,
                    'roll_weight_kg': str(roll_weight),
                    'wastage_kg': str(wastage),
                    'session_version_after_log': session.version + 1
                }
            )
            return log

    def clean(self):
        if getattr(self, 'session', None) and self.session.job_order.is_completed:
            raise ValidationError("Cannot log output. The associated Job Order is fully closed and completed.")
        
        if self.wastage_kg < Decimal('0') or self.wastage_kg > self.roll_weight_kg:
            raise ValidationError({'wastage_kg': 'Wastage cannot be negative or greater than the total roll weight.'})
        
        if self.session_id:
            total_reserved = sum(sm.reserved_kg for sm in self.session.materials.all())
            already_consumed = self.session.total_output_kg + self.session.total_wastage_kg
            remaining_material = max(Decimal('0'), total_reserved - already_consumed)
            attempted_consumption = self.roll_weight_kg + self.wastage_kg

            if self.pk is None and attempted_consumption > (remaining_material * Decimal('1.02')):
                raise ValidationError({
                    'roll_weight_kg': f'Physical limit exceeded: Attempting to log {attempted_consumption:.1f}kg, but only {remaining_material:.1f}kg of reserved material remains.'
                })

    def save(self, *args, **kwargs):
        self.clean()
        is_new = self.pk is None 
        super().save(*args, **kwargs)

        if is_new:
            session = self.session
            ExtrusionSession.objects.filter(pk=session.pk).update(
                total_output_kg=F('total_output_kg') + self.roll_weight_kg,
                total_wastage_kg=F('total_wastage_kg') + self.wastage_kg,
                version=F('version') + 1
            )
            JobOrder.objects.filter(pk=session.job_order.pk).update(
                total_extruded_kg=F('total_extruded_kg') + self.roll_weight_kg
            )
            
            session.refresh_from_db()
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
    
    input_roll_weight_kg = models.DecimalField(max_digits=10, decimal_places=2)
    
    status = models.CharField(max_length=20, default='ACTIVE', choices=[('ACTIVE', 'Active'), ('COMPLETED', 'Completed'), ('ENDED_EARLY', 'Ended Early')])
    
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)
    
    total_output_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_wastage_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    version = models.PositiveIntegerField(default=1)

    @classmethod
    @transaction.atomic
    def start_session(cls, job_order, machine_no, shift, input_roll, operator):
        if job_order.is_completed: 
            raise ValidationError("This Job Order is already completed.")
            
        remaining_extruded = job_order.total_extruded_kg - job_order.total_cut_kg - job_order.total_cutting_wastage_kg
        if input_roll > (remaining_extruded * Decimal('1.05')):
            raise ValidationError(f"Cannot mount {input_roll}kg roll. Only {remaining_extruded:.1f}kg remains from Extrusion.")
        
        return cls.objects.create(
            job_order=job_order, machine_no=machine_no, shift=shift, 
            input_roll_weight_kg=input_roll, operator_name=operator
        )

    def stop_session(self, calculate_wastage=True):
        if self.status != 'ACTIVE':
            return
            
        with transaction.atomic():
            self.end_time = timezone.now()
            
            if calculate_wastage:
                wastage = self.input_roll_weight_kg - self.total_output_kg
                if wastage > Decimal('0'):
                    self.total_wastage_kg = wastage
                    JobOrder.objects.filter(pk=self.job_order.pk).update(
                        total_cutting_wastage_kg=F('total_cutting_wastage_kg') + self.total_wastage_kg
                    )
                self.status = 'COMPLETED'
                
                # --- YIELD ADAPTATION ---
                if self.input_roll_weight_kg > Decimal('0') and self.job_order.recipe_id:
                    actual_yield = self.total_output_kg / self.input_roll_weight_kg
                    operator = self.operator_name or "System"
                    
                    Recipe.adapt_wastage_rate(
                        recipe_id=self.job_order.recipe_id,
                        stage='CUTTING',
                        observed_yield=actual_yield,
                        session=self,
                        operator_name=operator
                    )
            else:
                self.status = 'ENDED_EARLY'
                
            self.save(update_fields=['status', 'end_time', 'total_wastage_kg'])

    def __str__(self):
        return f"Cut Machine {self.machine_no} | JO: {self.job_order.jo_number} ({self.status})"

class CuttingLog(models.Model):
    session = models.ForeignKey(CuttingSession, on_delete=models.CASCADE, related_name='logs', null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    output_kg = models.DecimalField(max_digits=8, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])

    def clean(self):
        if getattr(self, 'session', None):
            if self.session.job_order.is_completed:
                raise ValidationError("Cannot log output. The associated Job Order is fully closed and completed.")
            if self.session.status != 'ACTIVE':
                raise ValidationError("This machine session is no longer active.")
            
        if self.session_id:
            if self.session.status != 'ACTIVE':
                raise ValidationError("This machine session is no longer active.")
            remaining = self.session.input_roll_weight_kg - self.session.total_output_kg
            if self.pk is None and self.output_kg > (remaining * Decimal('1.05')):
                raise ValidationError({'output_kg': f'Cannot log {self.output_kg}kg. Only {remaining:.1f}kg remains on this roll.'})

    def save(self, *args, **kwargs):
        self.clean()
        is_new = self.pk is None
        super().save(*args, **kwargs)
        
        if is_new:
            session = self.session
            CuttingSession.objects.filter(pk=session.pk).update(
                total_output_kg=F('total_output_kg') + self.output_kg,
                version=F('version') + 1
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
    
    packing_size_kg = models.DecimalField(max_digits=6, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    quantity_packed = models.IntegerField(validators=[MinValueValidator(1)])

    @classmethod
    def record_packing(cls, job_order, packing_size, quantity, operator):
        with transaction.atomic():
            jo = JobOrder.objects.select_for_update().get(pk=job_order.pk)
            if jo.is_completed:
                raise ValidationError("This Job Order is already closed or completed.")

            total_weight_submitting = packing_size * Decimal(str(quantity))
            remaining_to_pack = jo.total_cut_kg - jo.total_packed_kg

            if total_weight_submitting > (remaining_to_pack * Decimal('1.05')):
                raise ValidationError(f"Attempting to pack {total_weight_submitting:.1f}kg, but only {remaining_to_pack:.1f}kg is available.")

            log = cls.objects.create(
                job_order=jo,
                packing_size_kg=packing_size,
                quantity_packed=quantity,
                operator_name=operator
            )
            
            jo.refresh_from_db()
            if jo.total_packed_kg >= jo.order_quantity_kg:
                jo.complete_job()
                
            return log
        
    def clean(self):
        # NEW: Enforce the strict lockdown barrier
        if getattr(self, 'job_order', None) and self.job_order.is_completed:
            raise ValidationError("Cannot log packed goods. The associated Job Order is fully closed and completed.")

    def save(self, *args, **kwargs):
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
        # Enforce no updates post-completion
        if getattr(self, 'job_order', None) and self.job_order.is_completed:
            raise ValidationError("Cannot log dispatch. This Job Order is already closed or completed.")
            
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
            # Safely increment the shipped total
            JobOrder.objects.filter(pk=self.job_order.pk).update(
                total_shipped_kg=F('total_shipped_kg') + self.shipped_kg
            )
            
            # Auto-trigger completion evaluation
            # If the shipment falls short, it will cleanly abort and the job remains open.
            jo = JobOrder.objects.get(pk=self.job_order.pk)
            jo.complete_job()