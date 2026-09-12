from aiogram import BaseMiddleware
from auto_flipper.database import db, DatabaseBusyError


class AdminAccess(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, 'from_user', None)
        if user is None or not db.is_user_admin(user.id):
            if hasattr(event, 'answer'):
                await event.answer('Доступ только для ADMIN_IDS.')
            return None
        try:
            return await handler(event, data)
        except DatabaseBusyError:
            # A transaction may have failed after an external operation was sent.
            # Do not retry the business operation or claim that payment did not happen.
            message = getattr(event, 'message', None) or event
            if hasattr(message, 'answer'):
                await message.answer('База занята другим процессом. Автоматического повтора нет. '
                    'Проверьте /reconciliation и состояние операции перед повтором.')
            return None
