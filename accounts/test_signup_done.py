from django.test import TestCase
from django.urls import reverse


class SignupDonePageTests(TestCase):
    """The "check your email" page is the only feedback a player gets, so it has to explain silence."""

    def setUp(self):
        self.page = self.client.get(reverse("signup_done"))

    def test_the_page_explains_what_to_do_when_no_email_arrives(self):
        self.assertContains(self.page, "Didn’t get it?")
        self.assertContains(self.page, "spam and Promotions")
        self.assertContains(self.page, "spelled correctly")

    def test_an_existing_member_is_pointed_to_sign_in_and_password_reset(self):
        self.assertContains(self.page, "Already signed up before?")
        self.assertContains(self.page, f'href="{reverse("login")}"')
        self.assertContains(self.page, f'href="{reverse("password_reset")}"')

    def test_the_page_states_the_link_lifetime_and_the_resend_limits(self):
        self.assertContains(self.page, "30 minutes")
        self.assertContains(self.page, "two minutes")
        self.assertContains(self.page, "five times an hour")
        self.assertContains(self.page, f'href="{reverse("signup")}"')

    def test_the_page_is_identical_whether_or_not_an_account_exists(self):
        # It takes no email, so it cannot reveal which addresses are registered.
        self.assertEqual(self.client.get(reverse("signup_done")).content, self.page.content)
        self.assertEqual(self.page.status_code, 200)
