"""
Invitation email service for AnnotaHub.
Handles sending invitation emails to new users.
"""
import logging
from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes

logger = logging.getLogger(__name__)


def send_invitation_email(email, token, site_url=None):
    """
    Send an invitation email to a new user.
    
    Args:
        email: The recipient's email address
        token: The invitation token
        site_url: Base URL for the site (defaults to http://localhost:8000)
    
    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    if site_url is None:
        site_url = getattr(settings, 'SITE_URL', 'http://localhost:8000')
    
    # Build the invitation link
    invitation_link = f"{site_url}{reverse('comments:accept_invitation', kwargs={'token': token})}"
    
    subject = 'Mời bạn tham gia AnnotaHub'
    message = f"""Xin chào,

Bạn được mời tham gia dự án trên AnnotaHub.

Vui lòng nhấn vào liên kết bên dưới để hoàn tất đăng ký và bắt đầu sử dụng:

{invitation_link}

Liên kết này sẽ hết hạn sau 7 ngày.

Trân trọng,
Đội ngũ AnnotaHub"""
    
    html_message = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6;">
        <h2>Xin chào,</h2>
        <p>Bạn được mời tham gia dự án trên <strong>AnnotaHub</strong>.</p>
        <p>Vui lòng nhấn vào nút bên dưới để hoàn tất đăng ký và bắt đầu sử dụng:</p>
        <p>
            <a href="{invitation_link}" 
               style="background-color: #007bff; color: white; padding: 12px 24px; 
                      text-decoration: none; border-radius: 4px; display: inline-block;">
                Hoàn tất đăng ký
            </a>
        </p>
        <p>Hoặc sao chép liên kết này vào trình duyệt:</p>
        <p><code>{invitation_link}</code></p>
        <p><em>Liên kết này sẽ hết hạn sau 7 ngày.</em></p>
        <hr>
        <p>Trân trọng,<br>Đội ngũ AnnotaHub</p>
    </body>
    </html>
    """
    
    from_email = getattr(settings, 'EMAIL_FROM', settings.DEFAULT_FROM_EMAIL)
    
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=from_email,
            recipient_list=[email],
            html_message=html_message,
            fail_silently=False,
        )
        logger.info(f"Invitation email sent to {email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send invitation email to {email}: {e}")
        return False


def log_invitation_attempt(email, token, success):
    """
    Log an invitation attempt for debugging purposes.
    
    Args:
        email: The recipient's email address
        token: The invitation token
        success: Whether the email was sent successfully
    """
    status = "SUCCESS" if success else "FAILED"
    logger.info(f"Invitation {status} - Email: {email}, Token: {token[:8]}...")