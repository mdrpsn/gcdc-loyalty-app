from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.permissions import ADMIN, ORGANIZER, is_admin, is_organizer, is_player
from club.models import Attendance, AuditEvent, OpenPlay, Player, Registration, Venue
from club.services import (
    add_historical_attendance,
    cancel_openplay,
    cancel_registration,
    check_in,
    check_in_walk_in,
    complete_openplay,
    create_linked_player,
    create_openplay,
    create_venue,
    create_walk_in_player,
    find_players,
    link_player_account,
    openplays_attended,
    register_player,
    restore_attendance,
    void_attendance,
)


User = get_user_model()


class DomainTestCase(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.organizer = User.objects.create_user("organizer@example.com")
        self.organizer.groups.add(Group.objects.get(name=ORGANIZER))
        self.admin = User.objects.create_user("admin@example.com")
        self.admin.groups.add(Group.objects.get(name=ADMIN))
        self.member = User.objects.create_user("member@example.com")
        self.venue = create_venue(name="Main Court", actor=self.organizer)

    def session(self, *, starts_at=None):
        starts_at = starts_at or self.now - timedelta(minutes=15)
        return create_openplay(
            venue_id=self.venue.pk,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
            actor=self.organizer,
        )

    def linked_player(self):
        return create_linked_player(user_id=self.member.pk, display_name="Member", actor=self.member)

    def completed_session(self):
        session = self.session(starts_at=self.now - timedelta(hours=3))
        complete_openplay(openplay_id=session.pk, actor=self.organizer, now=self.now)
        return session


class PlayerAndVenueTests(DomainTestCase):
    def test_linked_player_uses_user_email_and_is_not_auto_claimed(self):
        walk_in = create_walk_in_player(
            display_name="Member", contact_email=self.member.email, actor=self.organizer
        )
        self.assertIsNone(walk_in.user_id)
        self.assertEqual(find_players(term=self.member.email, actor=self.organizer).count(), 1)

        linked = link_player_account(player_id=walk_in.pk, user_id=self.member.pk, actor=self.admin)
        self.assertEqual(linked.user_id, self.member.pk)
        self.assertIsNone(linked.contact_email)
        self.assertEqual(linked.user.email, self.member.email)
        self.assertTrue(AuditEvent.objects.filter(action="PLAYER_LINKED", target_id=walk_in.pk).exists())

    def test_linked_player_has_no_second_email(self):
        player = Player(user=self.member, display_name="Member", contact_email="   ")
        player.save()
        self.assertIsNone(player.contact_email)
        self.assertEqual(player.user.email, "member@example.com")

    def test_walk_in_requires_name_and_contact(self):
        with self.assertRaises(ValidationError):
            create_walk_in_player(display_name="", phone="123", actor=self.organizer)
        with self.assertRaises(ValidationError):
            create_walk_in_player(display_name="No Contact", actor=self.organizer)
        self.assertFalse(Player.objects.exists())

        player = create_walk_in_player(display_name="  Walk In  ", phone=" 09171234567 ", actor=self.organizer)
        self.assertEqual(player.display_name, "Walk In")
        self.assertEqual(player.phone, "09171234567")

    def test_database_rejects_invalid_identity_when_model_validation_is_bypassed(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Player.objects.bulk_create([Player(display_name="No Contact")])

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Player.objects.bulk_create(
                    [Player(user=self.member, display_name="Member", contact_email="other@example.com")]
                )

    def test_one_account_cannot_have_two_players(self):
        self.linked_player()
        with self.assertRaises(ValidationError):
            create_linked_player(user_id=self.member.pk, display_name="Duplicate", actor=self.member)

    def test_venue_can_be_deactivated_but_historical_reference_is_protected(self):
        session = self.session()
        self.venue.is_active = False
        self.venue.save(update_fields=["is_active", "updated_at"])
        self.assertFalse(Venue.objects.get(pk=self.venue.pk).is_active)
        with self.assertRaises(ValidationError):
            create_openplay(
                venue_id=self.venue.pk,
                starts_at=self.now + timedelta(days=1),
                ends_at=self.now + timedelta(days=1, hours=2),
                actor=self.organizer,
            )
        with self.assertRaises(ValidationError):
            self.venue.delete()
        self.assertEqual(session.venue_id, self.venue.pk)

    def test_role_checks_use_groups_and_inactive_accounts_are_denied(self):
        self.assertTrue(is_player(self.member))
        self.assertTrue(is_organizer(self.organizer))
        self.assertTrue(is_admin(self.admin))
        self.assertTrue(is_organizer(self.admin))
        self.assertFalse(is_organizer(self.member))
        self.organizer.is_active = False
        self.organizer.save()
        self.assertFalse(is_organizer(self.organizer))


class OpenPlayAndRegistrationTests(DomainTestCase):
    def test_openplay_lifecycle_and_audit(self):
        session = self.completed_session()
        session.refresh_from_db()
        self.assertEqual(session.status, OpenPlay.Status.COMPLETED)
        self.assertIsNotNone(session.completed_at)
        self.assertTrue(AuditEvent.objects.filter(action="OPENPLAY_COMPLETED", target_id=session.pk).exists())
        with self.assertRaises(ValidationError):
            complete_openplay(openplay_id=session.pk, actor=self.organizer)
        with self.assertRaises(PermissionDenied):
            cancel_openplay(openplay_id=session.pk, actor=self.organizer, reason="Correction")
        cancel_openplay(openplay_id=session.pk, actor=self.admin, reason="Session never happened")
        session.refresh_from_db()
        self.assertEqual(session.status, OpenPlay.Status.CANCELLED)
        self.assertTrue(AuditEvent.objects.filter(action="OPENPLAY_CANCELLED", target_id=session.pk).exists())

    def test_scheduled_cancellation_and_invalid_transition(self):
        session = self.session(starts_at=self.now + timedelta(days=1))
        self.assertEqual(session.status, OpenPlay.Status.SCHEDULED)
        cancel_openplay(openplay_id=session.pk, actor=self.organizer, reason="Rain")
        with self.assertRaises(ValidationError):
            cancel_openplay(openplay_id=session.pk, actor=self.organizer, reason="Again")
        with self.assertRaises(ValidationError):
            complete_openplay(openplay_id=session.pk, actor=self.organizer)

    def test_cannot_complete_before_scheduled_end(self):
        session = self.session()
        with self.assertRaises(ValidationError):
            complete_openplay(openplay_id=session.pk, actor=self.organizer, now=self.now)
        self.assertEqual(OpenPlay.objects.get(pk=session.pk).status, OpenPlay.Status.SCHEDULED)

    def test_registration_cancel_and_reactivate_never_count_as_attendance(self):
        player = self.linked_player()
        session = self.session(starts_at=self.now + timedelta(days=1))
        first = register_player(openplay_id=session.pk, player_id=player.pk, actor=self.member)
        repeated = register_player(openplay_id=session.pk, player_id=player.pk, actor=self.member)
        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(Registration.objects.filter(player=player, openplay=session).count(), 1)
        self.assertEqual(openplays_attended(player), 0)

        cancel_registration(openplay_id=session.pk, player_id=player.pk, actor=self.member)
        first.refresh_from_db()
        self.assertEqual(first.status, Registration.Status.CANCELLED)
        self.assertIsNotNone(first.cancelled_at)
        again = register_player(openplay_id=session.pk, player_id=player.pk, actor=self.member)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(again.status, Registration.Status.ACTIVE)
        self.assertIsNone(again.cancelled_at)
        self.assertEqual(openplays_attended(player), 0)
        self.assertEqual(AuditEvent.objects.filter(target_type="registration", target_id=first.pk).count(), 3)

    def test_registration_is_unique_at_database_level(self):
        player = self.linked_player()
        session = self.session(starts_at=self.now + timedelta(days=1))
        register_player(openplay_id=session.pk, player_id=player.pk, actor=self.member)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Registration.objects.bulk_create([Registration(player=player, openplay=session)])

    def test_player_cannot_register_another_player(self):
        other = create_walk_in_player(display_name="Other", phone="123", actor=self.organizer)
        session = self.session(starts_at=self.now + timedelta(days=1))
        with self.assertRaises(PermissionDenied):
            register_player(openplay_id=session.pk, player_id=other.pk, actor=self.member)


class AttendanceTests(DomainTestCase):
    def test_valid_check_in_is_idempotent_and_audited_once(self):
        player = self.linked_player()
        session = self.session()
        first, created = check_in(openplay_id=session.pk, player_id=player.pk, actor=self.organizer)
        second, created_again = check_in(openplay_id=session.pk, player_id=player.pk, actor=self.organizer)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.status, Attendance.Status.VALID)
        self.assertEqual(first.source, Attendance.Source.WALK_IN)
        self.assertEqual(openplays_attended(player), 1)
        self.assertEqual(AuditEvent.objects.filter(action="ATTENDANCE_CHECKED_IN", target_id=first.pk).count(), 1)

    def test_registered_check_in_has_roster_source(self):
        player = self.linked_player()
        session = self.session(starts_at=self.now + timedelta(minutes=10))
        register_player(openplay_id=session.pk, player_id=player.pk, actor=self.member)
        attendance, _ = check_in(openplay_id=session.pk, player_id=player.pk, actor=self.organizer)
        self.assertEqual(attendance.source, Attendance.Source.ROSTER)

    def test_walk_in_creation_and_check_in_are_atomic_and_count(self):
        session = self.session()
        player, attendance = check_in_walk_in(
            openplay_id=session.pk,
            display_name="Walk In",
            phone="09171234567",
            actor=self.organizer,
        )
        self.assertIsNone(player.user_id)
        self.assertEqual(attendance.player_id, player.pk)
        self.assertEqual(openplays_attended(player), 1)
        self.assertTrue(AuditEvent.objects.filter(action="PLAYER_CREATED", target_id=player.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action="ATTENDANCE_CHECKED_IN", target_id=attendance.pk).exists())

    def test_invalid_walk_in_rolls_back_player_and_attendance(self):
        session = self.session()
        with self.assertRaises(ValidationError):
            check_in_walk_in(openplay_id=session.pk, display_name="No Contact", actor=self.organizer)
        self.assertFalse(Player.objects.exists())
        self.assertFalse(Attendance.objects.exists())

    def test_void_restore_and_ordinary_check_in_cannot_restore(self):
        player = self.linked_player()
        session = self.session()
        attendance, _ = check_in(openplay_id=session.pk, player_id=player.pk, actor=self.organizer)
        with self.assertRaises(PermissionDenied):
            void_attendance(attendance_id=attendance.pk, actor=self.organizer, reason="Mistake")
        void_attendance(attendance_id=attendance.pk, actor=self.admin, reason="Mistake")
        self.assertEqual(openplays_attended(player), 0)
        with self.assertRaises(ValidationError):
            check_in(openplay_id=session.pk, player_id=player.pk, actor=self.organizer)
        restore_attendance(attendance_id=attendance.pk, actor=self.admin, reason="Verified")
        self.assertEqual(openplays_attended(player), 1)
        self.assertEqual(Attendance.objects.filter(player=player, openplay=session).count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action="ATTENDANCE_VOIDED", target_id=attendance.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action="ATTENDANCE_RESTORED", target_id=attendance.pk).exists())

    def test_database_rejects_duplicate_attendance(self):
        player = self.linked_player()
        session = self.session()
        check_in(openplay_id=session.pk, player_id=player.pk, actor=self.organizer)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Attendance.objects.bulk_create(
                    [Attendance(player=player, openplay=session, recorded_by=self.organizer, source=Attendance.Source.WALK_IN)]
                )

    def test_cancelled_session_attendance_does_not_count(self):
        player = self.linked_player()
        session = self.session()
        attendance, _ = check_in(openplay_id=session.pk, player_id=player.pk, actor=self.organizer)
        self.assertEqual(openplays_attended(player), 1)
        cancel_openplay(openplay_id=session.pk, actor=self.organizer, reason="Rain")
        self.assertEqual(openplays_attended(player), 0)
        self.assertTrue(Attendance.objects.filter(pk=attendance.pk).exists())

    def test_exceptional_completed_session_cancellation_removes_derived_count(self):
        player = self.linked_player()
        session = self.completed_session()
        add_historical_attendance(
            openplay_id=session.pk, player_id=player.pk, actor=self.admin, reason="Paper roster"
        )
        self.assertEqual(openplays_attended(player), 1)
        cancel_openplay(openplay_id=session.pk, actor=self.admin, reason="Recorded against wrong date")
        self.assertEqual(openplays_attended(player), 0)
        self.assertEqual(player.attendances.count(), 1)

    def test_multiple_sessions_same_day_and_completed_history_count(self):
        player = self.linked_player()
        first = self.session(starts_at=self.now - timedelta(minutes=20))
        second = self.session(starts_at=self.now - timedelta(minutes=10))
        check_in(openplay_id=first.pk, player_id=player.pk, actor=self.organizer)
        check_in(openplay_id=second.pk, player_id=player.pk, actor=self.organizer)
        self.assertEqual(openplays_attended(player), 2)
        complete_openplay(openplay_id=first.pk, actor=self.organizer, now=self.now + timedelta(hours=3))
        self.assertEqual(openplays_attended(player), 2)
        self.assertEqual(player.attendances.count(), 2)

    def test_historical_addition_counts_and_requires_admin_reason(self):
        player = self.linked_player()
        session = self.completed_session()
        with self.assertRaises(PermissionDenied):
            add_historical_attendance(
                openplay_id=session.pk, player_id=player.pk, actor=self.organizer, reason="Paper roster"
            )
        attendance = add_historical_attendance(
            openplay_id=session.pk, player_id=player.pk, actor=self.admin, reason="Paper roster"
        )
        self.assertEqual(attendance.source, Attendance.Source.HISTORICAL)
        self.assertEqual(openplays_attended(player), 1)
        self.assertTrue(AuditEvent.objects.filter(action="ATTENDANCE_ADDED", target_id=attendance.pk).exists())

    def test_player_cannot_check_in(self):
        player = self.linked_player()
        session = self.session()
        with self.assertRaises(PermissionDenied):
            check_in(openplay_id=session.pk, player_id=player.pk, actor=self.member)


class AuditAndIntegrityTests(DomainTestCase):
    def test_audit_events_are_append_only(self):
        event = AuditEvent.objects.first()
        event.action = "ALTERED"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()
        with self.assertRaises(ValidationError):
            AuditEvent.objects.filter(pk=event.pk).update(action="ALTERED")
        with self.assertRaises(ValidationError):
            AuditEvent.objects.filter(pk=event.pk).delete()

    def test_business_records_cannot_be_casually_deleted(self):
        player = self.linked_player()
        session = self.session()
        attendance, _ = check_in(openplay_id=session.pk, player_id=player.pk, actor=self.organizer)
        for record in (player, session, attendance):
            with self.assertRaises(ValidationError):
                record.delete()
            with self.assertRaises(ValidationError):
                type(record).objects.filter(pk=record.pk).delete()

    def test_audit_failure_rolls_back_check_in_and_completion(self):
        from unittest.mock import patch

        player = self.linked_player()
        session = self.session()
        with patch("club.services._audit", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                check_in(openplay_id=session.pk, player_id=player.pk, actor=self.organizer)
        self.assertFalse(Attendance.objects.filter(player=player, openplay=session).exists())

        past = self.session(starts_at=self.now - timedelta(hours=3))
        with patch("club.services._audit", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                complete_openplay(openplay_id=past.pk, actor=self.organizer, now=self.now)
        self.assertEqual(OpenPlay.objects.get(pk=past.pk).status, OpenPlay.Status.SCHEDULED)

    def test_walk_in_audit_failure_rolls_back_player_and_attendance(self):
        from unittest.mock import patch

        from club import services

        session = self.session()
        original_audit = services._audit
        before = AuditEvent.objects.count()

        def fail_on_check_in(action, target, actor, **details):
            if action == "ATTENDANCE_CHECKED_IN":
                raise RuntimeError("audit unavailable")
            return original_audit(action, target, actor, **details)

        with patch("club.services._audit", side_effect=fail_on_check_in):
            with self.assertRaises(RuntimeError):
                check_in_walk_in(
                    openplay_id=session.pk,
                    display_name="Walk In",
                    phone="09171234567",
                    actor=self.organizer,
                )

        self.assertFalse(Player.objects.exists())
        self.assertFalse(Attendance.objects.exists())
        self.assertEqual(AuditEvent.objects.count(), before)
