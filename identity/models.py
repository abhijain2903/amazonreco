from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.fields import ArrayField
from django.db import models

ROLE_CHOICES = [
    ("PIC", "Amazon account (PIC)"),
    ("Planning", "Planning"),
    ("Credit", "Credit control"),
    ("Logistics", "Logistics"),
    ("Product", "Product team"),
    ("Finance", "Finance"),
    ("Manager", "Manager"),
    ("Admin", "Admin"),
]
ROLE_TITLES = dict(ROLE_CHOICES)
ROLE_HOME = {"PIC": "action", "Planning": "pos", "Credit": "pos", "Logistics": "ship",
             "Product": "promos", "Finance": "pay", "Manager": "dashboard", "Admin": "settings"}


class User(AbstractUser):
    display_name = models.CharField(max_length=120, blank=True)
    roles = ArrayField(models.CharField(max_length=20, choices=ROLE_CHOICES), default=list, blank=True)

    @property
    def name(self):
        return self.display_name or self.get_full_name() or self.username

    @property
    def primary_role(self):
        return self.roles[0] if self.roles else "Manager"

    @property
    def role_title(self):
        return ROLE_TITLES.get(self.primary_role, self.primary_role)

    @property
    def initials(self):
        return "".join(w[0] for w in self.name.split()[:2]).upper()
