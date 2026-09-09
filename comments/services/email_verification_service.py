"""
Email verification service for AnnotaHub.
Handles sending verification emails for new user registrations.
"""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import override

logger = logging.getLogger(__name__)


def send_verification_email(email, token, language=None, site_url=None):
    """
    Send an email verification email to a newly registered user.

    Args:
        email: The recipient's email address
        token: The verification token
        site_url: Base URL for the site (defaults to SITE_URL setting)

    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    if site_url is None:
        site_url = getattr(settings, 'SITE_URL', 'http://localhost:8000')
    if language is None:
        from django.utils.translation import get_language
        language = get_language()
    # Build the verification link
    verification_link = f"{site_url}{reverse('comments:verify_email', kwargs={'token': token})}"
    from_email = getattr(settings, 'EMAIL_FROM', settings.DEFAULT_FROM_EMAIL)

    with override(language):
        subject = _("Email Verification - AnnotaHub")
        message = _(
            """Hello,

Thank you for registering an account on AnnotaHub.

Please click the link below to verify your email address:

{verification_link}

This link will expire in 7 days.

Best regards,

The AnnotaHub Team"""

        ).format(verification_link=verification_link)

        html_message = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6;">
            <h2>{_("Hello,")}</h2>
            <p>{_("Thank you for registering an account on")} <strong>AnnotaHub</strong>.</p>
            <p>{_("Please click the button below to verify your email address:")}</p>
            <p>
                <a href="{verification_link}"
                   style="background-color:#007bff;color:white;padding:12px 24px;
                          text-decoration:none;border-radius:4px;display:inline-block;">
                    {_("Verify Email")}
                </a>
            </p>
            <p>{_("Or copy and paste this link into your browser:")}</p>
            <p><code>{verification_link}</code></p>
            <p><em>{_("This link will expire in 7 days.")}</em></p>
            <hr>
            <p>{_("Best regards,")}<br>{_("The AnnotaHub Team")}</p>
        </body>
        </html>
        """

#     subject = 'Xác thực địa chỉ email - AnnotaHub'
#     message = f"""Xin chào,

# Cảm ơn bạn đã đăng ký tài khoản trên AnnotaHub.

# Vui lòng nhấn vào liên kết bên dưới để xác thực địa chỉ email của bạn:

# {verification_link}

# Liên kết này sẽ hết hạn sau 7 ngày.

# Trân trọng,
# Đội ngũ AnnotaHub"""

#     html_message = f"""
#     <html>
#     <body style="font-family: Arial, sans-serif; line-height: 1.6;">
#         <h2>Xin chào,</h2>
#         <p>Cảm ơn bạn đã đăng ký tài khoản trên <strong>AnnotaHub</strong>.</p>
#         <p>Vui lòng nhấn vào nút bên dưới để xác thực địa chỉ email của bạn:</p>
#         <p>
#             <a href="{verification_link}"
#                style="background-color: #007bff; color: white; padding: 12px 24px;
#                       text-decoration: none; border-radius: 4px; display: inline-block;">
#                 Xác thực email
#             </a>
#         </p>
#         <p>Hoặc sao chép liên kết này vào trình duyệt:</p>
#         <p><code>{verification_link}</code></p>
#         <p><em>Liên kết này sẽ hết hạn sau 7 ngày.</em></p>
#         <hr>
#         <p>Trân trọng,<br>Đội ngũ AnnotaHub</p>
#     </body>
#     </html>
#     """


    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=from_email,
            recipient_list=[email],
            html_message=html_message,
            fail_silently=False,
        )
        logger.info(f"Verification email sent to {email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send verification email to {email}: {e}")
        return False


def resend_verification_email(email, token, site_url=None):
    """
    Resend a verification email.

    Args:
        email: The recipient's email address
        token: The verification token
        site_url: Base URL for the site

    Returns:
        bool: True if email was sent successfully, False otherwise
    """
    return send_verification_email(email, token, site_url)
