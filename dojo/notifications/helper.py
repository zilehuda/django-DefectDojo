import requests
import logging
from django.core.mail import EmailMessage
from dojo.models import Notifications, Dojo_User, Alerts, UserContactInfo
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string
from django.db.models import Q, Count, Prefetch
from django.urls import reverse
from dojo.celery import app


logger = logging.getLogger(__name__)


def create_notification(event=None, *args, **kwargs):
    if 'recipients' in kwargs:
        # mimic existing code so that when recipients is specified, no other system or personal notifications are sent.
        logger.debug('creating notifications for recipients')
        for recipient_notifications in Notifications.objects.filter(user__username__in=kwargs['recipients'], user__is_active=True, product=None):
            # kwargs.update({'user': recipient_notifications.user})
            process_notifications(event, recipient_notifications, *args, **kwargs)
    else:
        logger.debug('creating system notifications')
        # send system notifications to all admin users

        # System notifications
        try:
            system_notifications = Notifications.objects.get(user=None)
        except Exception:
            system_notifications = Notifications()

        # System notifications are sent one with user=None, which will trigger email to configured system email, to global slack channel, etc.
        process_notifications(event, system_notifications, *args, **kwargs)

        # All admins will also receive system notifications, but as part of the person global notifications section below
        # This time user is set, so will trigger email to personal email, to personal slack channel (mention), etc.
        # only retrieve users which have at least one notification type enabled for this event type.
        logger.debug('creating personal notifications')

        product = None
        if 'product' in kwargs:
            product = kwargs.get('product')

        if not product and 'engagement' in kwargs:
            product = kwargs['engagement'].product

        if not product and 'test' in kwargs:
            product = kwargs['test'].engagement.product

        if not product and 'finding' in kwargs:
            product = kwargs['finding'].test.engagement.product

        # get users with either global notifications, or a product specific noditiciation
        # and all admin/superuser, they will always be notified
        users = Dojo_User.objects.filter(is_active=True).prefetch_related(Prefetch(
            "notifications_set",
            queryset=Notifications.objects.filter(Q(product_id=product) | Q(product__isnull=True)),
            to_attr="applicable_notifications"
        )).annotate(applicable_notifications_count=Count('notifications__id', filter=Q(notifications__product_id=product) | Q(notifications__product__isnull=True)))\
            .filter((Q(applicable_notifications_count__gt=0) | Q(is_superuser=True) | Q(is_staff=True)))

        # only send to authorized users or admin/superusers
        if product:
            users = users.filter(Q(id__in=product.authorized_users.all()) | Q(is_superuser=True) | Q(is_staff=True))

        for user in users:
            # send notifications to user after merging possible multiple notifications records (i.e. personal global + personal product)
            # kwargs.update({'user': user})
            applicable_notifications = user.applicable_notifications
            if user.is_staff or user.is_superuser:
                # admin users get all system notifications
                applicable_notifications.append(system_notifications)

            notifications_set = Notifications.merge_notifications_list(applicable_notifications)

            process_notifications(event, notifications_set, *args, **kwargs)


def create_description(event, *args, **kwargs):
    if "description" not in kwargs.keys():
        if event == 'product_added':
            kwargs["description"] = "Product " + kwargs['title'] + " has been created successfully."
        else:
            kwargs["description"] = "Event " + str(event) + " has occured."

    return kwargs["description"]


def create_notification_message(event, user, notification_type, *args, **kwargs):
    template = 'notifications/%s.tpl' % event.replace('/', '')
    kwargs.update({'type': notification_type})
    kwargs.update({'user': user})

    notification_message = None
    try:
        notification_message = render_to_string(template, kwargs)
    except TemplateDoesNotExist:
        logger.debug('template not found or not implemented yet: %s', template)
    except Exception as e:
        logger.error("error during rendeing of template %s exception is %s", template, e)
    finally:
        if not notification_message:
            kwargs["description"] = create_description(event, *args, **kwargs)
            notification_message = render_to_string('notifications/other.tpl', kwargs)

    return notification_message


def process_notifications(event, notifications=None, *args, **kwargs):
    from dojo.utils import get_system_setting

    if not notifications:
        logger.warn('no notifications!')
        return

    sync = 'initiator' in kwargs and hasattr(kwargs['initiator'], 'usercontactinfo') and kwargs['initiator'].usercontactinfo.block_execution

    # sync = True

    # logger.debug('sync: %s %s', sync, vars(notifications))
    logger.debug('sending notification ' + ('synchronously' if sync else 'asynchronously'))
    logger.debug('process notifications for %s', notifications.user)
    logger.debug('notifications: %s', vars(notifications))

    slack_enabled = get_system_setting('enable_slack_notifications')
    hipchat_enabled = get_system_setting('enable_hipchat_notifications')
    mail_enabled = get_system_setting('enable_mail_notifications')

    from dojo.tasks import send_slack_notification_task, send_hipchat_notification_task, send_mail_notification_task

    if slack_enabled and 'slack' in getattr(notifications, event):
        if not sync:
            send_slack_notification_task.delay(event, notifications.user, *args, **kwargs)
        else:
            send_slack_notification(event, notifications.user, *args, **kwargs)

    if hipchat_enabled and 'hipchat' in getattr(notifications, event):
        if not sync:
            send_hipchat_notification_task.delay(event, notifications.user, *args, **kwargs)
        else:
            send_hipchat_notification(event, notifications.user, *args, **kwargs)

    logger.debug('mail_enabled: %s', mail_enabled)
    logger.debug('getattr(notifications, event): %s', getattr(notifications, event))
    if mail_enabled and 'mail' in getattr(notifications, event):
        logger.debug('mailing!!')
        # print(f' Args: {args}')
        # print(f' Kwargs: {kwargs}')
        # print(event)
        # print(notifications.user)
        if not sync:
            send_mail_notification_task.delay(event, notifications.user, *args, **kwargs)
        else:
            send_mail_notification(event, notifications.user, *args, **kwargs)

    if 'alert' in getattr(notifications, event, None):
        send_alert_notification(event, notifications.user, *args, **kwargs)


@app.task(name='send_slack_notification')
def send_slack_notification(event, user=None, *args, **kwargs):
    from dojo.utils import get_system_setting

    def _post_slack_message(channel):
        res = requests.request(
            method='POST',
            url='https://slack.com/api/chat.postMessage',
            data={
                'token': get_system_setting('slack_token'),
                'channel': channel,
                'username': get_system_setting('slack_username'),
                'text': create_notification_message(event, user, 'slack', *args, **kwargs)
            })

        if 'error' in res.text:
            logger.error("Slack is complaining. See raw text below.")
            logger.error(res.text)
            raise RuntimeError('Error posting message to Slack: ' + res.text)

    try:
        # If the user has slack information on profile and chooses to receive slack notifications
        # Will receive a DM
        if user is not None:
            logger.debug('personal notification to slack for user %s', user)
            if hasattr(user, 'usercontactinfo') and user.usercontactinfo.slack_username is not None:
                slack_user_id = user.usercontactinfo.slack_user_id
                if not slack_user_id:
                    # Lookup the slack userid the first time, then save it.
                    slack_user_id = get_slack_user_id(
                        user.usercontactinfo.slack_username)

                    if slack_user_id:
                        slack_user_save = UserContactInfo.objects.get(user_id=user.id)
                        slack_user_save.slack_user_id = slack_user_id
                        slack_user_save.save()

                # only send notification if we managed to find the slack_user_id
                if slack_user_id:
                    channel = '@{}'.format(slack_user_id)
                    _post_slack_message(channel)
            else:
                logger.info("The user %s does not have a email address informed for Slack in profile.", user)
        else:
            # System scope slack notifications, and not personal would still see this go through
            if get_system_setting('slack_channel') is not None:
                channel = get_system_setting('slack_channel')
                logger.info("Sending system notification to system channel {}.".format(channel))
                _post_slack_message(channel)
            else:
                logger.debug('slack_channel not configured: skipping system notification')

    except Exception as e:
        logger.exception(e)
        log_alert(e, 'Slack Notification', title=kwargs['title'], description=str(e), url=kwargs['url'])


@app.task(name='send_hipchat_notification')
def send_hipchat_notification(event, user=None, *args, **kwargs):
    from dojo.utils import get_system_setting
    try:
        # HipChat doesn't seem to offer direct message functionality, so no HipChat PM functionality here...
        if not user:
            # We use same template for HipChat as for slack
            res = requests.request(
                method='POST',
                url='https://%s/v2/room/%s/notification?auth_token=%s' %
                (get_system_setting('hipchat_site'),
                get_system_setting('hipchat_channel'),
                get_system_setting('hipchat_token')),
                data={
                    'message': create_notification_message(event, 'slack', *args, **kwargs),
                    'message_format': 'text'
                })
    except Exception as e:
        logger.exception(e)
        log_alert(e, "HipChat Notification", title=kwargs['title'], description=str(e), url=kwargs['url'])
        pass


@app.task(name='send_mail_notification')
def send_mail_notification(event, user=None, *args, **kwargs):
    print('going to email finally')
    from dojo.utils import get_system_setting

    if user:
        address = user.email
    else:
        address = get_system_setting('mail_notifications_to')

    logger.debug('notification email for user %s to %s', user, address)

    try:
        subject = '%s notification' % get_system_setting('team_name')
        if 'title' in kwargs:
            subject += ': %s' % kwargs['title']

        email = EmailMessage(
            subject,
            create_notification_message(event, user, 'mail', *args, **kwargs),
            get_system_setting('mail_notifications_from'),
            [address],
            headers={"From": "{}".format(get_system_setting('mail_notifications_from'))}
        )
        email.content_subtype = 'html'
        logger.debug('sending email alert')
        # logger.info(create_notification_message(event, 'mail'))
        email.send(fail_silently=False)

    except Exception as e:
        logger.exception(e)
        log_alert(e, "Email Notification", title=kwargs['title'], description=str(e), url=kwargs['url'])
        pass


def send_alert_notification(event, user=None, *args, **kwargs):
    logger.info('sending alert notification to %s', user)
    try:
        # no need to differentiate between user/no user
        icon = kwargs.get('icon', 'info-circle')
        alert = Alerts(
            user_id=user,
            title=kwargs.get('title')[:100],
            description=create_notification_message(event, user, 'alert', *args, **kwargs),
            url=kwargs.get('url', reverse('alerts')),
            icon=icon[:25],
            source=Notifications._meta.get_field(event).verbose_name.title()[:100]
        )
        # relative urls will fail validation
        alert.clean_fields(exclude=['url'])
        alert.save()
    except Exception as e:
        logger.exception(e)
        log_alert(e, "Alert Notification", title=kwargs['title'], description=str(e), url=kwargs['url'])
        pass


def get_slack_user_id(user_email):
    user_id = None

    res = requests.request(
        method='POST',
        url='https://slack.com/api/users.list',
        data={'token': get_system_setting('slack_token')})

    users = json.loads(res.text)

    slack_user_is_found = False
    if users:
        if 'error' in users:
            logger.error("Slack is complaining. See error message below.")
            logger.error(users)
            raise RuntimeError('Error getting user list from Slack: ' + res.text)
        else:
            for member in users["members"]:
                if "email" in member["profile"]:
                    if user_email == member["profile"]["email"]:
                        if "id" in member:
                            user_id = member["id"]
                            logger.debug("Slack user ID is {}".format(user_id))
                            slack_user_is_found = True
                            break
                    else:
                        logger.warn("A user with email {} could not be found in this Slack workspace.".format(user_email))

            if not slack_user_is_found:
                logger.warn("The Slack user was not found.")

    return user_id


def log_alert(e, notification_type=None, *args, **kwargs):
    # no try catch here, if this fails we need to show an error

    users = Dojo_User.objects.filter((Q(is_superuser=True) | Q(is_staff=True)))
    for user in users:
        alert = Alerts(
            user_id=user,
            url=kwargs.get('url', reverse('alerts'))[:100],
            title=kwargs.get('title', 'Notification issue'),
            description=kwargs.get('description', '%s' % e)[:2000],
            icon="exclamation-triangle",
            source=notification_type[:100] if notification_type else kwargs.get('source', 'unknown')[:100])
        # relative urls will fail validation
        print('alert.description:', alert.description)
        alert.clean_fields(exclude=['url'])
        alert.save()
