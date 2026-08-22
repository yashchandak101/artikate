from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import Asset, CheckOut, Employee

ASSETS = [
    ("AST-001", "Sony A7 III", Asset.Category.CAMERA),
    ("AST-002", "Canon R5", Asset.Category.CAMERA),
    ("AST-003", "Dell XPS 15", Asset.Category.LAPTOP),
    ("AST-004", "MacBook Pro 16", Asset.Category.LAPTOP),
    ("AST-005", "LIDAR Scanner Unit", Asset.Category.SENSOR),
    ("AST-006", "Thermal Imaging Camera", Asset.Category.SENSOR),
    ("AST-007", "Field Survey Van 1", Asset.Category.VEHICLE),
    ("AST-008", "Field Survey Van 2", Asset.Category.VEHICLE),
]

EMPLOYEES = [
    ("EMP-001", "Asha Mehta", "asha.mehta@artikate.example", True),
    ("EMP-002", "Rohan Gupta", "rohan.gupta@artikate.example", True),
    ("EMP-003", "Priya Nair", "priya.nair@artikate.example", True),
    ("EMP-004", "Vikram Singh", "vikram.singh@artikate.example", False),
]


class Command(BaseCommand):
    help = "Seed the database with demo assets, employees and check-outs."

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now()

        assets = {}
        for tag, name, category in ASSETS:
            asset, created = Asset.objects.get_or_create(
                asset_tag=tag,
                defaults={
                    "name": name,
                    "category": category,
                    "purchase_date": (now - timedelta(days=400)).date(),
                },
            )
            assets[tag] = asset
            self.stdout.write(
                self.style.SUCCESS(f"Asset {tag}: {'created' if created else 'already exists'}")
            )

        employees = {}
        for code, full_name, email, is_active in EMPLOYEES:
            employee, created = Employee.objects.get_or_create(
                employee_code=code,
                defaults={"full_name": full_name, "email": email, "is_active": is_active},
            )
            employees[code] = employee
            self.stdout.write(
                self.style.SUCCESS(f"Employee {code}: {'created' if created else 'already exists'}")
            )

        # (asset_tag, employee_code, checked_out_days_ago, due_days_ago (negative = future),
        #  returned_days_ago or None, needs_maintenance)
        # "SEED:" prefix in condition_note is the idempotency key: we look
        # for an existing check-out with this exact note before creating
        # one, so re-running the command doesn't pile up duplicate rows.
        scenarios = [
            # -- currently overdue (open, due date in the past) --
            ("AST-001", "EMP-001", 10, 3, None, False, "SEED: overdue-1"),
            ("AST-003", "EMP-002", 20, 5, None, False, "SEED: overdue-2"),
            # -- returned on time (returned before due date) --
            ("AST-005", "EMP-003", 20, 10, 12, False, "SEED: on-time-1"),
            ("AST-006", "EMP-003", 15, 5, 7, False, "SEED: on-time-2"),
            # -- returned late (returned after due date) --
            ("AST-007", "EMP-002", 25, 15, 10, False, "SEED: late-1"),
        ]

        for (
            tag,
            code,
            out_days_ago,
            due_days_ago,
            returned_days_ago,
            needs_maint,
            marker,
        ) in scenarios:
            existing = CheckOut.objects.filter(
                asset=assets[tag], employee=employees[code], condition_note=marker
            ).first()
            if existing:
                self.stdout.write(
                    self.style.WARNING(f"Checkout '{marker}' already exists, skipping")
                )
                continue

            checked_out_at = now - timedelta(days=out_days_ago)
            due_at = (
                now - timedelta(days=due_days_ago)
                if due_days_ago >= 0
                else now + timedelta(days=-due_days_ago)
            )
            returned_at = (
                now - timedelta(days=returned_days_ago) if returned_days_ago is not None else None
            )

            checkout = CheckOut.objects.create(
                asset=assets[tag],
                employee=employees[code],
                due_at=due_at,
                condition_note=marker,
            )
            # auto_now_add locks checked_out_at on first save; overwrite via
            # a plain UPDATE so we can backdate demo history.
            CheckOut.objects.filter(pk=checkout.pk).update(
                checked_out_at=checked_out_at, returned_at=returned_at
            )

            asset = assets[tag]
            if returned_at is None:
                asset.status = Asset.Status.CHECKED_OUT
            else:
                asset.status = Asset.Status.MAINTENANCE if needs_maint else Asset.Status.AVAILABLE
            asset.save(update_fields=["status", "updated_at"])

            self.stdout.write(self.style.SUCCESS(f"Checkout '{marker}' created"))

        self.stdout.write(self.style.SUCCESS("Seed data complete."))
