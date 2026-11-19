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

class ExtrusionSession(models.Model):
    job_order = models.ForeignKey('production.JobOrder', on_delete=models.CASCADE, related_name='extrusion_sessions')
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
                
                from production.models import Recipe 
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
        
        # --- FIX: PROPORTIONAL ROLLOVER FOR MIXED MATERIALS ---
        # Instead of dumping the entire balance into the first material, we preserve the exact physical mix ratio.
        if total_reserved > Decimal('0') and remaining_balance > Decimal('0'):
            for sm in self.materials.all():
                proportion = sm.reserved_kg / total_reserved
                transferred_amount = remaining_balance * proportion
                
                if transferred_amount > Decimal('0'):
                    SessionMaterial.objects.create(
                        session=new_session,
                        material=sm.material,
                        reserved_kg=transferred_amount
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
    session = models.ForeignKey('production.ExtrusionSession', on_delete=models.CASCADE, related_name='materials')
    material = models.ForeignKey('production.RawMaterial', on_delete=models.CASCADE)
    reserved_kg = models.DecimalField(max_digits=10, decimal_places=2)
    actual_used_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

class ExtrusionLog(models.Model):
    session = models.ForeignKey('production.ExtrusionSession', on_delete=models.CASCADE, related_name='rolls')
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
    job_order = models.ForeignKey('production.JobOrder', on_delete=models.CASCADE, related_name='cutting_sessions')
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
    session = models.ForeignKey('production.CuttingSession', on_delete=models.CASCADE, related_name='logs', null=True, blank=True)
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

