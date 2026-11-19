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
    recipe = models.ForeignKey('production.Recipe', on_delete=models.SET_NULL, null=True, blank=True)
    
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
            
                from production.signals import trigger_supervisor_alert # Lazy import
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
            from production.models import AuditLog 
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
        # Finished goods + all recorded scrap + returned mixed material
        returned_mixed = sum(s.returned_material_kg for s in self.extrusion_sessions.all())
        return self.total_packed_kg + self.total_extrusion_wastage_kg + self.total_cutting_wastage_kg + returned_mixed

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
    job_order = models.ForeignKey('production.JobOrder', on_delete=models.CASCADE, related_name='allocations')
    material = models.ForeignKey('production.RawMaterial', on_delete=models.CASCADE)
    
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
    job_order = models.ForeignKey('production.JobOrder', on_delete=models.CASCADE, related_name='usage_logs')
    material = models.ForeignKey('production.RawMaterial', on_delete=models.CASCADE)
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

