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

class MaterialCategory(models.Model):
    name = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "Material Categories"

    def __str__(self):
        return self.name

class RawMaterial(models.Model):
    category = models.ForeignKey('production.MaterialCategory', on_delete=models.SET_NULL, null=True, blank=True, related_name='materials')
    material_id = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    current_stock_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    reorder_point_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('100.00'))

    def __str__(self):
        return f"{self.material_id} - {self.name}"

class MaterialRestockLog(models.Model):
    material = models.ForeignKey('production.RawMaterial', on_delete=models.CASCADE, related_name='restocks')
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

