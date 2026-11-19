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

class PackingLog(models.Model):
    job_order = models.ForeignKey('production.JobOrder', on_delete=models.CASCADE, related_name='packing_logs')
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
    job_order = models.ForeignKey('production.JobOrder', on_delete=models.CASCADE, related_name='shipments')
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

