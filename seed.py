# import os
# import logging
# from datetime import datetime, timezone
# import uuid

# from db import get_db
# from auth import hash_password
# from models import UserInDB, Template

# log = logging.getLogger('seed')


# DEFAULT_TEMPLATES = [
#     {
#         'name': 'Welcome & Confirmation',
#         'category': 'Welcome',
#         'body': 'Hi {{name}}, this is the BYD Melbourne & Fairfield delivery team. Congrats on your new {{vehicle}}! Your delivery is booked for {{date}}. Reply anytime if you have questions.',
#     },
#     {
#         'name': 'Day-Before Reminder',
#         'category': 'Reminder',
#         'body': 'Hi {{name}}, friendly reminder \u2014 your {{vehicle}} delivery is tomorrow ({{date}}). Please bring your driver licence and proof of insurance. See you soon!',
#     },
#     {
#         'name': 'Ready for Pickup',
#         'category': 'Status',
#         'body': 'Great news {{name}}! Your {{vehicle}} has passed inspection and is ready for pickup. Please confirm your preferred time on {{date}}.',
#     },
#     {
#         'name': 'Vehicle Arrived',
#         'category': 'Status',
#         'body': 'Hi {{name}}, your {{vehicle}} has arrived at our delivery centre. We\u2019ll be in touch shortly to book your handover.',
#     },
#     {
#         'name': 'Post-Delivery Follow-up',
#         'category': 'Follow-up',
#         'body': 'Hi {{name}}, hope you\u2019re loving your new {{vehicle}}! Any questions, just reply here. We\u2019d appreciate a quick Google review if you have a moment.',
#     },
# ]


# async def bootstrap():
#     db = get_db()

#     # 1) Super admin
#     super_email = (os.environ.get('SUPER_ADMIN_EMAIL') or '').lower()
#     super_pw = os.environ.get('INITIAL_ADMIN_PASSWORD') or ''
#     super_name = os.environ.get('SUPER_ADMIN_NAME') or 'Super Admin'
#     force_reset = (os.environ.get('RESET_SUPER_ADMIN_PASSWORD') or '').lower() in ('1', 'true', 'yes')

#     if not super_email:
#         log.warning('SUPER_ADMIN_EMAIL not set \u2014 skipping super admin seed')
#     elif not super_pw:
#         log.warning('INITIAL_ADMIN_PASSWORD not set \u2014 skipping super admin seed')
#     else:
#         existing = await db.users.find_one({'email': super_email})
#         if not existing:
#             user = UserInDB(
#                 email=super_email,
#                 name=super_name,
#                 role='super_admin',
#                 password_hash=hash_password(super_pw),
#                 must_change_password=True,
#             )
#             await db.users.insert_one(user.model_dump(mode='json'))
#             log.info('Seeded super admin: %s', super_email)
#         else:
#             updates = {}
#             if existing.get('role') != 'super_admin':
#                 updates['role'] = 'super_admin'
#             if not existing.get('active', True):
#                 updates['active'] = True
#             if force_reset:
#                 updates['password_hash'] = hash_password(super_pw)
#                 updates['must_change_password'] = True
#                 log.info('RESET_SUPER_ADMIN_PASSWORD=true \u2014 password reset for %s', super_email)
#             if updates:
#                 await db.users.update_one({'id': existing['id']}, {'$set': updates})
#                 log.info('Updated super admin %s: %s', super_email, list(updates.keys()))
#             else:
#                 log.info('Super admin %s already present, no changes', super_email)

#     # 2) Default SMS templates
#     if await db.templates.count_documents({}) == 0:
#         for t in DEFAULT_TEMPLATES:
#             tpl = Template(**t)
#             await db.templates.insert_one(tpl.model_dump(mode='json'))
#         log.info('Seeded %d default templates', len(DEFAULT_TEMPLATES))
