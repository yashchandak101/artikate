import logging

from celery import shared_task
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import CheckOut, OverdueNotice

logger = logging.getLogger(__name__)


@shared_task
def flag_overdue_checkouts():
    """Create an OverdueNotice for every open, overdue check-out.

    Idempotent by construction: OverdueNotice has a unique constraint on
    (checkout, notice_date), so re-running this task multiple times on the
    same day creates at most one notice per overdue check-out. We rely on
    the database constraint rather than an application-level "already
    notified" flag, because the constraint is what actually holds under
    concurrent/retried task execution.
    """
    today = timezone.localdate()
    now = timezone.now()

    overdue_checkout_ids = list(
        CheckOut.objects.filter(
            returned_at__isnull=True, due_at__lt=now
        ).values_list("id", flat=True)
    )

    created = 0
    skipped = 0
    for checkout_id in overdue_checkout_ids:
        try:
            with transaction.atomic():
                OverdueNotice.objects.create(checkout_id=checkout_id, notice_date=today)
            created += 1
        except IntegrityError:
            # A notice for this checkout+day already exists (either from an
            # earlier run today, or a concurrent worker). That's expected
            # and not an error.
            skipped += 1

    logger.info(
        "flag_overdue_checkouts: %d overdue, %d notices created, %d already existed",
        len(overdue_checkout_ids),
        created,
        skipped,
    )
    return {"overdue": len(overdue_checkout_ids), "created": created, "skipped": skipped}
