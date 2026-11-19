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

        from production.signals import yield_adapted 
        
        # Fire the signal to update the Control Tower
        yield_adapted.send(
            sender=cls,
            recipe_id=recipe_id,
            stage=stage,
            new_wastage=new_wastage
        )

class RecipeItem(models.Model):
    recipe = models.ForeignKey('production.Recipe', on_delete=models.CASCADE, related_name='ingredients')
    material = models.ForeignKey('production.RawMaterial', on_delete=models.CASCADE)
    ratio = models.DecimalField(max_digits=5, decimal_places=4)

    def clean(self):
        if self.ratio <= Decimal('0') or self.ratio > Decimal('1'):
            raise ValidationError({'ratio': 'Ratio must be strictly between 0 and 1 (0% to 100%).'})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.recipe.formula_code} -> {self.material.name} ({self.ratio * Decimal('100')}%)"

