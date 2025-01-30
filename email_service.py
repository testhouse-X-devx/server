# email_service.py
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, To, Content
from datetime import datetime
import os

class EmailService:
    def __init__(self):
    # Initialize SendGrid client with API key
        
        # Create Email object for sender
        self.from_email = Email('krishna@devxconsultancy.com')

    def send_email(self, to_email, subject, content):
        """
        Send an email to a specified email address using SendGrid

        Args:
            to_email (str): Email address to send email to
            subject (str): Email subject
            content (str): Email content as HTML

        Returns:
            bool: Success of email send
        """
        try:
            print(f"Sending email to {to_email}")
            to_email = To(to_email)
            content = Content("text/html", content)
            mail = Mail(self.from_email, to_email, subject, content)
            print(f"Sending email to {mail}")
            response = self.sg.client.mail.send.post(request_body=mail.get())
            print(f"Email sent successfully to {to_email}. Status: {response.status_code}")
            return True
        except Exception as e:
            print(f"Error sending email: {(e)}")
            return False

    def send_trial_expiration_notice(self, user_email, days_remaining):
        subject = "Your Trial Period is Ending Soon"
        content = f"""
        <h2>Your Trial Period is Ending Soon</h2>
        <p>Hello,</p>
        <p>Your trial period will expire in {days_remaining} days. To continue using our services, 
        please upgrade to one of our subscription plans.</p>
        <p><a href="{os.getenv('FRONTEND_URL')}/pricing">View Our Plans</a></p>
        """
        return self.send_email(user_email, subject, content)

    def send_payment_blocked_notice(self, user_email, due_date):
        subject = "Action Required: Payment Overdue"
        content = f"""
        <h2>Payment Required to Restore Access</h2>
        <p>Hello,</p>
        <p>Your subscription has been suspended due to a missed payment. To restore access, 
        please complete the payment by {due_date.strftime('%B %d, %Y')}.</p>
        <p>After this date, your remaining credits and scans will be reset.</p>
        """
        return self.send_email(user_email, subject, content)

    def send_subscription_cancelled_notice(self, user_email, benefits_end_date):
        subject = "Subscription Cancelled - Benefits Period Active"
        content = f"""
        <h2>Subscription Cancellation Confirmed</h2>
        <p>Hello,</p>
        <p>Your subscription has been cancelled. You can continue to use your remaining credits 
        and scans until {benefits_end_date.strftime('%B %d, %Y')}.</p>
        <p>You can resubscribe at any time to continue using our services.</p>
        """
        return self.send_email(user_email, subject, content)

    def send_benefits_expiring_notice(self, user_email, days_remaining):
        subject = "Your Benefits Period is Ending Soon"
        content = f"""
        <h2>Benefits Period Ending Soon</h2>
        <p>Hello,</p>
        <p>Your benefits period will expire in {days_remaining} days. After this, your remaining 
        credits and scans will no longer be accessible.</p>
        <p>To continue using our services, please consider resubscribing.</p>
        <p><a href="{os.getenv('FRONTEND_URL')}/pricing">View Our Plans</a></p>
        """
        return self.send_email(user_email, subject, content)

    def send_payment_successful_notice(self, user_email, plan_name, amount):
        subject = "Payment Successful"
        content = f"""
        <h2>Payment Successfully Processed</h2>
        <p>Hello,</p>
        <p>We've successfully processed your payment of {amount} for the {plan_name} plan.</p>
        <p>Your subscription is now active and your account has been updated with the new credits and scans.</p>
        """
        return self.send_email(user_email, subject, content)