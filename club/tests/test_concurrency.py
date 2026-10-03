from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone

from accounts.permissions import ORGANIZER
from club.models import Attendance, OpenPlay, Player, Venue
from club.services import check_in, openplays_attended


class ConcurrentCheckInTests(TransactionTestCase):
    def test_two_check_ins_create_one_attendance(self):
        organizer = get_user_model().objects.create_user("organizer@example.com")
        organizer.groups.add(Group.objects.get_or_create(name=ORGANIZER)[0])
        player = Player.objects.create(display_name="Walk In", phone="09171234567")
        venue = Venue.objects.create(name="Main Court")
        now = timezone.now()
        session = OpenPlay.objects.create(
            venue=venue,
            starts_at=now - timedelta(minutes=10),
            ends_at=now + timedelta(hours=1),
        )
        barrier = Barrier(2)

        def attempt():
            try:
                barrier.wait(timeout=10)
                _, created = check_in(openplay_id=session.pk, player_id=player.pk, actor=organizer)
                return created
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))

        self.assertCountEqual(results, [True, False])
        self.assertEqual(Attendance.objects.filter(player=player, openplay=session).count(), 1)
        self.assertEqual(openplays_attended(player), 1)

