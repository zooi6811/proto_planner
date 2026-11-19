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

