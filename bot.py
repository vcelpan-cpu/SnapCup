import asyncio
import enum
import logging
import os
from collections import defaultdict
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Enum, ForeignKey, Integer,
    Numeric, String, UniqueConstraint, func, select, text
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

logging.basicConfig(level=logging.INFO)

# ============================== КОНФИГ ==============================

BOT_TOKEN = os.environ["BOT_TOKEN"]

_raw_db_url = os.environ["DATABASE_URL"]
if _raw_db_url.startswith("postgresql+asyncpg://"):
    DATABASE_URL = _raw_db_url
elif _raw_db_url.startswith("postgresql://"):
    DATABASE_URL = _raw_db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    DATABASE_URL = _raw_db_url.replace("postgres://", "postgresql+asyncpg://", 1)

# Чат/группа кухни, куда падают отчёты, заявки и уведомления о заказах
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])

# ID телеграм-аккаунтов, которые могут управлять каталогом и подтверждать кофейни
ADMIN_USER_IDS = {int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()}

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))

# Время, после которого приём заказов на сегодня закрывается и уходит отчёт на кухню
CUTOFF_HOUR = int(os.environ.get("CUTOFF_HOUR", 15))
CUTOFF_MINUTE = int(os.environ.get("CUTOFF_MINUTE", 0))

# За сколько минут до кат-оффа напомнить кофейням, которые ещё не заказали (0 — выключить)
REMINDER_BEFORE_MINUTES = int(os.environ.get("REMINDER_BEFORE_MINUTES", 60))
_cutoff_total_minutes = CUTOFF_HOUR * 60 + CUTOFF_MINUTE
_reminder_total_minutes = (_cutoff_total_minutes - REMINDER_BEFORE_MINUTES) % (24 * 60)
REMINDER_HOUR = _reminder_total_minutes // 60
REMINDER_MINUTE = _reminder_total_minutes % 60

# Контакты кондитерской для кнопки "Связь" у кофеен (необязательно)
CONFECTIONERY_CONTACT = os.environ.get("CONFECTIONERY_CONTACT", "")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def today() -> date:
    return datetime.now(TZ).date()


# ============================== БАЗА ДАННЫХ ==============================

class Base(DeclarativeBase):
    pass


class ShopStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    blocked = "blocked"


class OrderStatus(str, enum.Enum):
    draft = "draft"          # кофейня ещё собирает
    submitted = "submitted"  # кофейня оформила, ждёт решения кухни
    confirmed = "confirmed"  # кухня приняла в работу
    completed = "completed"  # выполнен и доставлен/забран
    cancelled = "cancelled"  # отменён (кофейней или кухней)
    locked = "locked"        # кат-офф прошёл


class CoffeeShop(Base):
    __tablename__ = "coffee_shops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    address: Mapped[str] = mapped_column(String(256), default="")
    contact: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[ShopStatus] = mapped_column(Enum(ShopStatus), default=ShopStatus.pending)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    orders: Mapped[list["Order"]] = relationship(back_populates="shop")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(32), default="шт")
    price: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("shop_id", "order_date", name="uq_shop_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("coffee_shops.id"))
    order_date: Mapped[date] = mapped_column(Date)
    # Статус хранится простой строкой (не Postgres ENUM) — так новые статусы
    # можно добавлять в коде без ручных миграций базы.
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.draft.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    shop: Mapped["CoffeeShop"] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (UniqueConstraint("order_id", "product_id", name="uq_order_product"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    qty: Mapped[int] = mapped_column(Integer, default=0)

    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship()


engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Лёгкие авто-миграции для баз, созданных до этих полей/изменений.
        await conn.execute(text(
            "ALTER TABLE coffee_shops ADD COLUMN IF NOT EXISTS address VARCHAR(256) DEFAULT ''"
        ))
        await conn.execute(text(
            "ALTER TABLE coffee_shops ADD COLUMN IF NOT EXISTS contact VARCHAR(128) DEFAULT ''"
        ))
        # orders.status раньше был Postgres ENUM — переводим в обычную строку,
        # чтобы новые статусы (confirmed/cancelled) не требовали миграций впредь.
        await conn.execute(text(
            "ALTER TABLE orders ALTER COLUMN status TYPE VARCHAR(20) USING status::text"
        ))


async def get_session() -> AsyncSession:
    return SessionLocal()


async def get_shop(tg_id: int) -> CoffeeShop | None:
    async with await get_session() as session:
        result = await session.execute(select(CoffeeShop).where(CoffeeShop.tg_id == tg_id))
        return result.scalar_one_or_none()


async def get_or_create_draft(session, shop_id: int) -> Order:
    result = await session.execute(
        select(Order).where(Order.shop_id == shop_id, Order.order_date == today())
    )
    order = result.scalar_one_or_none()
    if order is None:
        order = Order(shop_id=shop_id, order_date=today(), status=OrderStatus.draft.value)
        session.add(order)
        await session.flush()
    elif order.status == OrderStatus.cancelled.value:
        # Кофейня отменила заказ — даём собрать новый с чистого листа.
        items = (await session.execute(
            select(OrderItem).where(OrderItem.order_id == order.id)
        )).scalars().all()
        for i in items:
            i.qty = 0
        order.status = OrderStatus.draft.value
        await session.flush()
    return order


# ============================== КНОПОЧНЫЕ МЕНЮ ==============================

def shop_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🧾 Заказ"), KeyboardButton(text="📋 Статус")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📞 Связь")],
            [KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
    )


def admin_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Отчёт"), KeyboardButton(text="📋 Заказы")],
            [KeyboardButton(text="☕ Кофейни"), KeyboardButton(text="🍰 Каталог")],
            [KeyboardButton(text="➕ Товар"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
    )


def shop_detail_lines(shop: CoffeeShop) -> list[str]:
    lines = []
    if shop.address:
        lines.append(f"📍 {shop.address}")
    if shop.contact:
        lines.append(f"📞 {shop.contact}")
    return lines


ORDER_STATUS_LABELS = {
    "draft": "черновик",
    "submitted": "оформлен, ждёт решения",
    "confirmed": "✅ принят кухней",
    "completed": "🎉 выполнен",
    "cancelled": "❌ отменён",
    "locked": "🔒 закрыт, на кухне",
}


# ============================== ХЕНДЛЕРЫ: КОФЕЙНИ ==============================

shop_router = Router()


class Registration(StatesGroup):
    waiting_name = State()
    waiting_address = State()
    waiting_contact = State()


@shop_router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    if is_admin(message.from_user.id):
        await message.answer(
            "🛠 Привет! Это админ-панель бота кондитерской.\n"
            "Меню кнопок ниже, или /help — список всех команд.",
            reply_markup=admin_menu_kb(),
        )
        return

    shop = await get_shop(message.from_user.id)
    if shop is None:
        await state.set_state(Registration.waiting_name)
        await message.answer(
            "👋 Привет! Это бот приёма заказов для кондитерской.\n\n"
            "Как называется ваша кофейня? Напишите название одним сообщением."
        )
        return
    if shop.status == ShopStatus.pending:
        await message.answer("⏳ Заявка на подключение ещё на рассмотрении. Мы сообщим, как только подтвердят.")
        return
    if shop.status == ShopStatus.blocked:
        await message.answer("🚫 Доступ к боту для вашей точки закрыт. Свяжитесь с кондитерской напрямую.")
        return
    await message.answer(
        f"С возвращением, {shop.name}! 🧁",
        reply_markup=shop_menu_kb(),
    )


@shop_router.message(Registration.waiting_name)
async def save_shop_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:128])
    await state.set_state(Registration.waiting_address)
    await message.answer("📍 Адрес кофейни? (улица, дом — для доставки/самовывоза)")


@shop_router.message(Registration.waiting_address)
async def save_shop_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text.strip()[:256])
    await state.set_state(Registration.waiting_contact)
    await message.answer("📞 Контактный телефон или имя ответственного за приём заказов?")


@shop_router.message(Registration.waiting_contact)
async def save_shop_contact(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    name = data["name"]
    address = data["address"]
    contact = message.text.strip()[:128]

    async with await get_session() as session:
        shop = CoffeeShop(
            tg_id=message.from_user.id, name=name, address=address,
            contact=contact, status=ShopStatus.pending,
        )
        session.add(shop)
        await session.commit()
        await session.refresh(shop)

    await state.clear()
    await message.answer("✅ Заявка отправлена. Ждите подтверждения — я напишу сразу, как одобрят.")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"shop_approve:{shop.id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"shop_reject:{shop.id}"),
    ]])
    detail_text = "\n".join(shop_detail_lines(shop))
    await bot.send_message(
        ADMIN_CHAT_ID,
        f"🆕 Новая заявка от кофейни: <b>{name}</b>\n{detail_text}\nTG ID: {message.from_user.id}",
        reply_markup=kb,
    )


def build_catalog_kb(products: list[Product], qty_map: dict[int, int]) -> InlineKeyboardMarkup:
    rows = []
    for p in products:
        qty = qty_map.get(p.id, 0)
        rows.append([
            InlineKeyboardButton(text="➖", callback_data=f"qty_dec:{p.id}"),
            InlineKeyboardButton(text=f"{p.name} · {qty} {p.unit}", callback_data="noop"),
            InlineKeyboardButton(text="➕", callback_data=f"qty_inc:{p.id}"),
        ])
    rows.append([InlineKeyboardButton(text="✅ Оформить заказ", callback_data="order_submit")])
    rows.append([InlineKeyboardButton(text="🔁 Повторить вчера", callback_data="order_repeat_yesterday")])
    rows.append([InlineKeyboardButton(text="❌ Отменить заказ", callback_data="order_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@shop_router.message(Command("order"))
@shop_router.message(F.text == "🧾 Заказ")
async def cmd_order(message: Message):
    shop = await get_shop(message.from_user.id)
    if shop is None or shop.status != ShopStatus.approved:
        await message.answer("Сначала нужно подтверждение доступа. Отправьте /start.")
        return

    async with await get_session() as session:
        order = await get_or_create_draft(session, shop.id)
        await session.commit()
        if order.status == OrderStatus.locked.value:
            await message.answer("⏰ Приём заказов на сегодня уже закрыт. Загляните завтра.")
            return

        products = (await session.execute(
            select(Product).where(Product.active == True).order_by(Product.sort_order)
        )).scalars().all()
        if not products:
            await message.answer("Каталог пока пуст — обратитесь к кондитерской.")
            return

        items = (await session.execute(
            select(OrderItem).where(OrderItem.order_id == order.id)
        )).scalars().all()
        qty_map = {i.product_id: i.qty for i in items}

    await message.answer(
        "🧾 Соберите заказ на сегодня кнопками ниже, потом нажмите «Оформить».\n"
        "Заказ можно менять до кат-оффа.",
        reply_markup=build_catalog_kb(products, qty_map),
    )


@shop_router.callback_query(F.data.startswith("qty_"))
async def adjust_qty(callback: CallbackQuery):
    action, product_id = callback.data.split(":")
    product_id = int(product_id)
    delta = 1 if action == "qty_inc" else -1

    shop = await get_shop(callback.from_user.id)
    if shop is None or shop.status != ShopStatus.approved:
        await callback.answer("Нет доступа", show_alert=True)
        return

    async with await get_session() as session:
        order = await get_or_create_draft(session, shop.id)
        if order.status == OrderStatus.locked.value:
            await callback.answer("Приём заказов закрыт", show_alert=True)
            return

        result = await session.execute(
            select(OrderItem).where(OrderItem.order_id == order.id, OrderItem.product_id == product_id)
        )
        item = result.scalar_one_or_none()
        if item is None:
            item = OrderItem(order_id=order.id, product_id=product_id, qty=0)
            session.add(item)
            await session.flush()
        item.qty = max(0, item.qty + delta)
        await session.commit()

        products = (await session.execute(
            select(Product).where(Product.active == True).order_by(Product.sort_order)
        )).scalars().all()
        items = (await session.execute(
            select(OrderItem).where(OrderItem.order_id == order.id)
        )).scalars().all()
        qty_map = {i.product_id: i.qty for i in items}

    await callback.message.edit_reply_markup(reply_markup=build_catalog_kb(products, qty_map))
    await callback.answer()


@shop_router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


@shop_router.callback_query(F.data == "order_submit")
async def submit_order(callback: CallbackQuery, bot: Bot):
    shop = await get_shop(callback.from_user.id)
    async with await get_session() as session:
        order = await get_or_create_draft(session, shop.id)
        was_submitted_before = order.status in (OrderStatus.submitted.value, OrderStatus.confirmed.value)
        rows = (await session.execute(
            select(OrderItem, Product).join(Product).where(
                OrderItem.order_id == order.id, OrderItem.qty > 0
            )
        )).all()
        if not rows:
            await callback.answer("Заказ пустой — добавьте хотя бы одну позицию", show_alert=True)
            return
        order.status = OrderStatus.submitted.value
        await session.commit()

    header = "✏️ Заказ изменён" if was_submitted_before else "🆕 Новый заказ"
    item_lines = [f"• {p.name}: {i.qty} {p.unit}" for i, p in rows]
    detail_lines = shop_detail_lines(shop)
    detail_text = ("\n" + "\n".join(detail_lines)) if detail_lines else ""
    await bot.send_message(
        ADMIN_CHAT_ID,
        f"{header} — <b>{shop.name}</b>{detail_text}\n\n" + "\n".join(item_lines),
    )

    await callback.message.answer(
        "✅ Заказ оформлен и передан в очередь на кухню.\n"
        "До кат-оффа его ещё можно поменять командой /order."
    )
    await callback.answer()


@shop_router.callback_query(F.data == "order_cancel")
async def cancel_order_by_shop(callback: CallbackQuery, bot: Bot):
    shop = await get_shop(callback.from_user.id)
    async with await get_session() as session:
        order = await get_or_create_draft(session, shop.id)
        if order.status == OrderStatus.locked.value:
            await callback.answer("Приём заказов уже закрыт, отменить нельзя", show_alert=True)
            return
        if order.status == OrderStatus.draft.value:
            await callback.answer("Заказ и так пустой — отменять нечего", show_alert=True)
            return

        items = (await session.execute(
            select(OrderItem).where(OrderItem.order_id == order.id)
        )).scalars().all()
        for i in items:
            i.qty = 0
        order.status = OrderStatus.cancelled.value
        await session.commit()

    await bot.send_message(ADMIN_CHAT_ID, f"❌ Заказ отменён кофейней — <b>{shop.name}</b>")
    await callback.message.answer("Заказ отменён. Когда будете готовы — /order, чтобы собрать заново.")
    await callback.answer()


@shop_router.callback_query(F.data == "order_repeat_yesterday")
async def repeat_yesterday(callback: CallbackQuery):
    shop = await get_shop(callback.from_user.id)
    if shop is None or shop.status != ShopStatus.approved:
        await callback.answer("Нет доступа", show_alert=True)
        return

    yesterday = today() - timedelta(days=1)
    async with await get_session() as session:
        order = await get_or_create_draft(session, shop.id)
        if order.status == OrderStatus.locked.value:
            await callback.answer("Приём заказов закрыт", show_alert=True)
            return

        yesterday_order = (await session.execute(
            select(Order).where(Order.shop_id == shop.id, Order.order_date == yesterday)
        )).scalar_one_or_none()
        if yesterday_order is None:
            await callback.answer("Вчера заказа не было", show_alert=True)
            return

        yesterday_items = (await session.execute(
            select(OrderItem).where(OrderItem.order_id == yesterday_order.id, OrderItem.qty > 0)
        )).scalars().all()
        if not yesterday_items:
            await callback.answer("Вчера заказ был пустым", show_alert=True)
            return

        for y_item in yesterday_items:
            result = await session.execute(
                select(OrderItem).where(OrderItem.order_id == order.id, OrderItem.product_id == y_item.product_id)
            )
            item = result.scalar_one_or_none()
            if item is None:
                item = OrderItem(order_id=order.id, product_id=y_item.product_id, qty=0)
                session.add(item)
                await session.flush()
            item.qty = y_item.qty
        await session.commit()

        products = (await session.execute(
            select(Product).where(Product.active == True).order_by(Product.sort_order)
        )).scalars().all()
        items = (await session.execute(
            select(OrderItem).where(OrderItem.order_id == order.id)
        )).scalars().all()
        qty_map = {i.product_id: i.qty for i in items}

    await callback.message.edit_reply_markup(reply_markup=build_catalog_kb(products, qty_map))
    await callback.answer("Заказ заполнен как вчера — проверьте и оформите")


@shop_router.message(Command("status"))
@shop_router.message(F.text == "📋 Статус")
async def cmd_status(message: Message):
    shop = await get_shop(message.from_user.id)
    if shop is None or shop.status != ShopStatus.approved:
        await message.answer("Сначала нужно подтверждение доступа. Отправьте /start.")
        return

    async with await get_session() as session:
        order = await get_or_create_draft(session, shop.id)
        await session.commit()
        items = (await session.execute(
            select(OrderItem, Product).join(Product).where(
                OrderItem.order_id == order.id, OrderItem.qty > 0
            )
        )).all()

    if not items:
        await message.answer("На сегодня заказ пока пустой. /order — чтобы собрать.")
        return

    lines = [f"• {p.name}: {i.qty} {p.unit}" for i, p in items]
    status_label = ORDER_STATUS_LABELS.get(order.status, order.status)
    await message.answer(f"📋 Заказ на сегодня ({status_label}):\n" + "\n".join(lines))


@shop_router.message(F.text == "📞 Связь")
async def contact_button(message: Message):
    shop = await get_shop(message.from_user.id)
    if shop is None or shop.status != ShopStatus.approved:
        await message.answer("Сначала нужно подтверждение доступа. Отправьте /start.")
        return
    contact_text = CONFECTIONERY_CONTACT or "Свяжитесь с менеджером кондитерской напрямую по известному вам номеру."
    await message.answer(f"📞 Связь с кондитерской:\n{contact_text}")


@shop_router.message(F.text == "📊 Статистика")
async def shop_stats_button(message: Message):
    shop = await get_shop(message.from_user.id)
    if shop is None or shop.status != ShopStatus.approved:
        await message.answer("Сначала нужно подтверждение доступа. Отправьте /start.")
        return

    async with await get_session() as session:
        rows = (await session.execute(
            select(Order, OrderItem).join(OrderItem, OrderItem.order_id == Order.id).where(
                Order.shop_id == shop.id, OrderItem.qty > 0, Order.status != OrderStatus.cancelled.value
            )
        )).all()

    if not rows:
        await message.answer("📊 Пока нет ни одного заказа в истории.")
        return

    order_dates = {o.order_date for o, _ in rows}
    total_items = sum(i.qty for _, i in rows)
    month_start = today().replace(day=1)
    month_items = sum(i.qty for o, i in rows if o.order_date >= month_start)

    await message.answer(
        "📊 <b>Ваша статистика</b>\n\n"
        f"Всего заказов: {len(order_dates)}\n"
        f"Всего позиций заказано: {total_items}\n"
        f"В этом месяце позиций: {month_items}"
    )


# ============================== ХЕНДЛЕРЫ: АДМИН ==============================

admin_router = Router()


class AddProduct(StatesGroup):
    waiting_name = State()
    waiting_unit = State()
    waiting_price = State()


@admin_router.message(Command("help"))
@admin_router.message(F.text == "❓ Помощь")
async def help_cmd(message: Message):
    if is_admin(message.from_user.id):
        await message.answer(
            "🛠 <b>Команды администратора</b>\n\n"
            "<b>Заказы</b>\n"
            "📋 Заказы / /orders — сегодняшние заказы с кнопками принять/выполнено/отменить\n"
            "📦 Отчёт / /report — сводка на сегодня\n\n"
            "<b>Кофейни</b>\n"
            "☕ Кофейни / /shops — список с кнопками подтвердить/заблокировать\n"
            "/approve &lt;id&gt;, /block &lt;id&gt; — то же текстом (резерв)\n\n"
            "<b>Каталог</b>\n"
            "🍰 Каталог / /catalog — список позиций, тап включает/выключает\n"
            "➕ Товар — добавить позицию через диалог\n"
            "/add_product Название; единица; цена — одной командой\n\n"
            "<b>Статистика</b>\n"
            "📊 Статистика / /stats — сводка за сегодня и за неделю\n\n"
            "Уведомления о новых заявках, новых и изменённых заказах приходят сюда автоматически.\n"
            f"За {REMINDER_BEFORE_MINUTES} мин. до кат-оффа кофейням без заказа уходит напоминание.\n"
            "Кат-офф и итоговый отчёт — каждый день в заданное время.",
            reply_markup=admin_menu_kb(),
        )
        return

    shop = await get_shop(message.from_user.id)
    if shop is None:
        await message.answer("👋 Отправьте /start, чтобы подать заявку на подключение вашей кофейни.")
        return
    await message.answer(
        "☕ <b>Команды кофейни</b>\n\n"
        "🧾 Заказ / /order — собрать, изменить или отменить заказ на сегодня\n"
        "   (в самом заказе есть кнопка «🔁 Повторить вчера»)\n"
        "📋 Статус / /status — что уже в заказе\n"
        "📊 Статистика — ваша история заказов\n"
        "📞 Связь — контакты кондитерской\n\n"
        "Заказ можно менять до кат-оффа, дальше он уходит на кухню.",
        reply_markup=shop_menu_kb(),
    )


@admin_router.callback_query(F.data.startswith("shop_approve:"))
async def approve_shop(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админов", show_alert=True)
        return
    shop_id = int(callback.data.split(":")[1])
    async with await get_session() as session:
        shop = await session.get(CoffeeShop, shop_id)
        shop.status = ShopStatus.approved
        await session.commit()
        tg_id = shop.tg_id

    await callback.message.edit_text(callback.message.text + "\n\n✅ Подтверждено")
    await bot.send_message(
        tg_id,
        "🎉 Заявка подтверждена! Собирайте заказ кнопкой ниже.",
        reply_markup=shop_menu_kb(),
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("shop_reject:"))
async def reject_shop(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админов", show_alert=True)
        return
    shop_id = int(callback.data.split(":")[1])
    async with await get_session() as session:
        shop = await session.get(CoffeeShop, shop_id)
        shop.status = ShopStatus.blocked
        await session.commit()
        tg_id = shop.tg_id

    await callback.message.edit_text(callback.message.text + "\n\n❌ Отклонено")
    await bot.send_message(tg_id, "К сожалению, заявку отклонили. Свяжитесь с кондитерской напрямую.")
    await callback.answer()


def build_shop_card_kb(shop: CoffeeShop) -> InlineKeyboardMarkup:
    if shop.status == ShopStatus.pending:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"shop_approve:{shop.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"shop_reject:{shop.id}"),
        ]])
    if shop.status == ShopStatus.approved:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"shop_block:{shop.id}"),
        ]])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔓 Разблокировать", callback_data=f"shop_unblock:{shop.id}"),
    ]])


@admin_router.callback_query(F.data.startswith("shop_block:"))
async def block_shop_cb(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админов", show_alert=True)
        return
    shop_id = int(callback.data.split(":")[1])
    async with await get_session() as session:
        shop = await session.get(CoffeeShop, shop_id)
        shop.status = ShopStatus.blocked
        await session.commit()
        await session.refresh(shop)
    await callback.message.edit_reply_markup(reply_markup=build_shop_card_kb(shop))
    await callback.answer("Заблокировано")


@admin_router.callback_query(F.data.startswith("shop_unblock:"))
async def unblock_shop_cb(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админов", show_alert=True)
        return
    shop_id = int(callback.data.split(":")[1])
    async with await get_session() as session:
        shop = await session.get(CoffeeShop, shop_id)
        shop.status = ShopStatus.approved
        await session.commit()
        await session.refresh(shop)
    await callback.message.edit_reply_markup(reply_markup=build_shop_card_kb(shop))
    await callback.answer("Разблокировано")


@admin_router.message(Command("shops"))
@admin_router.message(F.text == "☕ Кофейни")
async def list_shops(message: Message):
    if not is_admin(message.from_user.id):
        return
    async with await get_session() as session:
        shops = (await session.execute(select(CoffeeShop).order_by(CoffeeShop.id))).scalars().all()
    if not shops:
        await message.answer("Кофеен пока нет.", reply_markup=admin_menu_kb())
        return

    await message.answer("☕ Список кофеен:", reply_markup=admin_menu_kb())
    labels = {"pending": "⏳", "approved": "✅", "blocked": "🚫"}
    for s in shops:
        detail_lines = shop_detail_lines(s)
        detail_text = ("\n" + "\n".join(detail_lines)) if detail_lines else ""
        text_ = f"{labels[s.status.value]} #{s.id} <b>{s.name}</b>{detail_text}"
        await message.answer(text_, reply_markup=build_shop_card_kb(s))


@admin_router.message(Command("block"))
async def block_shop(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        shop_id = int(message.text.split(maxsplit=1)[1])
    except (IndexError, ValueError):
        await message.answer("Использование: /block <id кофейни>")
        return
    async with await get_session() as session:
        shop = await session.get(CoffeeShop, shop_id)
        if not shop:
            await message.answer("Не найдено")
            return
        shop.status = ShopStatus.blocked
        await session.commit()
    await message.answer(f"🚫 {shop.name} заблокирована")


@admin_router.message(Command("approve"))
async def approve_shop_cmd(message: Message, bot: Bot):
    """Ручное подтверждение заявки текстом — на случай, если кнопка в чат не пришла."""
    if not is_admin(message.from_user.id):
        return
    try:
        shop_id = int(message.text.split(maxsplit=1)[1])
    except (IndexError, ValueError):
        await message.answer("Использование: /approve <id кофейни> (см. /shops)")
        return
    async with await get_session() as session:
        shop = await session.get(CoffeeShop, shop_id)
        if not shop:
            await message.answer("Не найдено")
            return
        shop.status = ShopStatus.approved
        await session.commit()
        tg_id = shop.tg_id
        name = shop.name
    await message.answer(f"✅ {name} подтверждена")
    try:
        await bot.send_message(tg_id, "🎉 Заявка подтверждена! Собирайте заказ кнопкой ниже.", reply_markup=shop_menu_kb())
    except Exception:
        pass


@admin_router.message(F.text == "➕ Товар")
async def add_product_button(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AddProduct.waiting_name)
    await message.answer("Название новой позиции?")


@admin_router.message(AddProduct.waiting_name)
async def add_product_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:128])
    await state.set_state(AddProduct.waiting_unit)
    await message.answer("Единица измерения? (например: шт, порция, кг)")


@admin_router.message(AddProduct.waiting_unit)
async def add_product_unit(message: Message, state: FSMContext):
    await state.update_data(unit=message.text.strip()[:32])
    await state.set_state(AddProduct.waiting_price)
    await message.answer("Цена? (число, например 150)")


@admin_router.message(AddProduct.waiting_price)
async def add_product_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("Не похоже на число. Введите цену ещё раз, например: 150")
        return
    data = await state.get_data()
    async with await get_session() as session:
        session.add(Product(name=data["name"], unit=data["unit"], price=price))
        await session.commit()
    await state.clear()
    await message.answer(f"➕ Добавлено: {data['name']} ({data['unit']}, {price}₽)", reply_markup=admin_menu_kb())


@admin_router.message(Command("add_product"))
async def add_product_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        payload = message.text.split(maxsplit=1)[1]
        name, unit, price = [p.strip() for p in payload.split(";")]
    except (IndexError, ValueError):
        await message.answer("Использование: /add_product Название; единица; цена\nНапример: /add_product Круассан; шт; 120")
        return
    async with await get_session() as session:
        session.add(Product(name=name, unit=unit, price=float(price)))
        await session.commit()
    await message.answer(f"➕ Добавлено: {name} ({unit}, {price}₽)")


def build_catalog_admin_kb(products: list[Product]) -> InlineKeyboardMarkup:
    rows = []
    for p in products:
        label = f"{'✅' if p.active else '🚫'} {p.name} — {p.price}₽/{p.unit}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"toggle_product:{p.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.message(Command("catalog"))
@admin_router.message(F.text == "🍰 Каталог")
async def show_catalog(message: Message):
    if not is_admin(message.from_user.id):
        return
    async with await get_session() as session:
        products = (await session.execute(select(Product).order_by(Product.sort_order))).scalars().all()
    if not products:
        await message.answer("Каталог пуст. Нажмите «➕ Товар», чтобы добавить первую позицию.", reply_markup=admin_menu_kb())
        return
    await message.answer(
        "🍰 Каталог — нажмите на позицию, чтобы включить/выключить её для кофеен:",
        reply_markup=build_catalog_admin_kb(products),
    )


@admin_router.callback_query(F.data.startswith("toggle_product:"))
async def toggle_product_cb(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админов", show_alert=True)
        return
    product_id = int(callback.data.split(":")[1])
    async with await get_session() as session:
        product = await session.get(Product, product_id)
        if not product:
            await callback.answer("Не найдено", show_alert=True)
            return
        product.active = not product.active
        await session.commit()
        products = (await session.execute(select(Product).order_by(Product.sort_order))).scalars().all()
    await callback.message.edit_reply_markup(reply_markup=build_catalog_admin_kb(products))
    await callback.answer()


@admin_router.message(Command("toggle_product"))
async def toggle_product(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        product_id = int(message.text.split(maxsplit=1)[1])
    except (IndexError, ValueError):
        await message.answer("Использование: /toggle_product <id>")
        return
    async with await get_session() as session:
        product = await session.get(Product, product_id)
        if not product:
            await message.answer("Не найдено")
            return
        product.active = not product.active
        await session.commit()
        state = "включена" if product.active else "выключена"
    await message.answer(f"Позиция «{product.name}» теперь {state}")


# ---------- Обзор заказов на сегодня с принятием/отменой ----------

def build_order_card_kb(order: Order) -> InlineKeyboardMarkup | None:
    if order.status == OrderStatus.submitted.value:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Принять", callback_data=f"order_confirm:{order.id}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"order_cancel_admin:{order.id}"),
        ]])
    if order.status in (OrderStatus.confirmed.value, OrderStatus.locked.value):
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎉 Выполнено", callback_data=f"order_complete:{order.id}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"order_cancel_admin:{order.id}"),
        ]])
    return None


@admin_router.message(Command("orders"))
@admin_router.message(F.text == "📋 Заказы")
async def list_orders(message: Message):
    if not is_admin(message.from_user.id):
        return

    async with await get_session() as session:
        orders = (await session.execute(
            select(Order, CoffeeShop).join(CoffeeShop, Order.shop_id == CoffeeShop.id)
            .where(Order.order_date == today(), Order.status != OrderStatus.draft.value)
            .order_by(Order.status, CoffeeShop.name)
        )).all()

    if not orders:
        await message.answer("На сегодня пока нет оформленных заказов.", reply_markup=admin_menu_kb())
        return

    await message.answer("📋 Заказы на сегодня:", reply_markup=admin_menu_kb())
    async with await get_session() as session:
        for order, shop in orders:
            items = (await session.execute(
                select(OrderItem, Product).join(Product).where(
                    OrderItem.order_id == order.id, OrderItem.qty > 0
                )
            )).all()
            item_lines = "\n".join(f"• {p.name}: {i.qty} {p.unit}" for i, p in items)
            label = ORDER_STATUS_LABELS.get(order.status, order.status)
            text_ = f"☕ <b>{shop.name}</b> — {label}\n{item_lines}"
            await message.answer(text_, reply_markup=build_order_card_kb(order))


@admin_router.callback_query(F.data.startswith("order_confirm:"))
async def confirm_order_cb(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админов", show_alert=True)
        return
    order_id = int(callback.data.split(":")[1])
    async with await get_session() as session:
        order = await session.get(Order, order_id)
        if not order:
            await callback.answer("Не найдено", show_alert=True)
            return
        order.status = OrderStatus.confirmed.value
        await session.commit()
        shop = await session.get(CoffeeShop, order.shop_id)

    await callback.message.edit_reply_markup(reply_markup=build_order_card_kb(order))
    await callback.answer("Принято")
    try:
        await bot.send_message(shop.tg_id, "✅ Ваш заказ на сегодня принят кухней в работу.")
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("order_cancel_admin:"))
async def cancel_order_admin_cb(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админов", show_alert=True)
        return
    order_id = int(callback.data.split(":")[1])
    async with await get_session() as session:
        order = await session.get(Order, order_id)
        if not order:
            await callback.answer("Не найдено", show_alert=True)
            return
        order.status = OrderStatus.cancelled.value
        await session.commit()
        shop = await session.get(CoffeeShop, order.shop_id)

    await callback.message.edit_reply_markup(reply_markup=build_order_card_kb(order))
    await callback.answer("Отменено")
    try:
        await bot.send_message(
            shop.tg_id,
            "❌ Кондитерская отменила ваш заказ на сегодня. Свяжитесь для уточнения деталей.",
        )
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("order_complete:"))
async def complete_order_cb(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Только для админов", show_alert=True)
        return
    order_id = int(callback.data.split(":")[1])
    async with await get_session() as session:
        order = await session.get(Order, order_id)
        if not order:
            await callback.answer("Не найдено", show_alert=True)
            return
        order.status = OrderStatus.completed.value
        await session.commit()
        shop = await session.get(CoffeeShop, order.shop_id)

    await callback.message.edit_reply_markup(reply_markup=build_order_card_kb(order))
    await callback.answer("Отмечено как выполнено")
    try:
        await bot.send_message(shop.tg_id, "🎉 Ваш заказ на сегодня выполнен. Спасибо!")
    except Exception:
        pass


# ---------- Сводный отчёт (кат-офф и по требованию) ----------

async def build_report_text() -> str | None:
    async with await get_session() as session:
        rows = (await session.execute(
            select(Order, CoffeeShop, OrderItem, Product)
            .join(CoffeeShop, Order.shop_id == CoffeeShop.id)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .join(Product, OrderItem.product_id == Product.id)
            .where(
                Order.order_date == today(), OrderItem.qty > 0,
                Order.status != OrderStatus.cancelled.value,
            )
        )).all()

    if not rows:
        return None

    totals = defaultdict(int)
    by_shop = defaultdict(list)
    for order, shop, item, product in rows:
        totals[product.name] += item.qty
        by_shop[shop.name].append(f"{product.name}: {item.qty} {product.unit}")

    lines = [f"📦 Сводный заказ на {today().strftime('%d.%m.%Y')}\n"]
    for name, qty in sorted(totals.items()):
        lines.append(f"• {name}: {qty}")

    lines.append("\n— по кофейням —")
    for shop_name, items in sorted(by_shop.items()):
        lines.append(f"\n☕ {shop_name}:")
        lines.extend(f"  {i}" for i in items)

    return "\n".join(lines)


async def remind_unordered_shops(bot: Bot):
    """Напоминает кофейням, у которых на сегодня ещё нет оформленного заказа."""
    async with await get_session() as session:
        shops = (await session.execute(
            select(CoffeeShop).where(CoffeeShop.status == ShopStatus.approved)
        )).scalars().all()

        for shop in shops:
            order = (await session.execute(
                select(Order).where(Order.shop_id == shop.id, Order.order_date == today())
            )).scalar_one_or_none()
            already_ordered = order is not None and order.status in (
                OrderStatus.submitted.value, OrderStatus.confirmed.value,
                OrderStatus.locked.value, OrderStatus.completed.value,
            )
            if already_ordered:
                continue
            try:
                await bot.send_message(
                    shop.tg_id,
                    f"⏰ Напоминание: заказ на сегодня ещё не оформлен.\n"
                    f"Приём закрывается в {CUTOFF_HOUR:02d}:{CUTOFF_MINUTE:02d}. "
                    f"Нажмите «🧾 Заказ», чтобы успеть.",
                )
            except Exception:
                pass


async def lock_and_report(bot: Bot, admin_chat_id: int):
    async with await get_session() as session:
        orders = (await session.execute(
            select(Order).where(
                Order.order_date == today(),
                Order.status.notin_([
                    OrderStatus.locked.value, OrderStatus.cancelled.value, OrderStatus.completed.value,
                ]),
            )
        )).scalars().all()
        for order in orders:
            order.status = OrderStatus.locked.value
        await session.commit()

    text_ = await build_report_text()
    if text_ is None:
        text_ = f"📦 Заказов на {today().strftime('%d.%m.%Y')} нет."
    await bot.send_message(admin_chat_id, text_)


@admin_router.message(Command("report"))
@admin_router.message(F.text == "📦 Отчёт")
async def manual_report(message: Message):
    if not is_admin(message.from_user.id):
        return
    text_ = await build_report_text()
    await message.answer(text_ or "Заказов на сегодня пока нет.", reply_markup=admin_menu_kb())


# ---------- Статистика ----------

@admin_router.message(Command("stats"))
@admin_router.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    if not is_admin(message.from_user.id):
        return

    week_start = today() - timedelta(days=6)
    async with await get_session() as session:
        rows = (await session.execute(
            select(Order, OrderItem, Product)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .join(Product, OrderItem.product_id == Product.id)
            .where(Order.order_date >= week_start, OrderItem.qty > 0, Order.status != OrderStatus.cancelled.value)
        )).all()

    if not rows:
        await message.answer("📊 За последнюю неделю заказов не было.", reply_markup=admin_menu_kb())
        return

    today_rows = [r for r in rows if r[0].order_date == today()]
    today_shops = {r[0].shop_id for r in today_rows}
    today_qty = sum(r[1].qty for r in today_rows)
    today_revenue = sum(float(r[2].price) * r[1].qty for r in today_rows)

    week_orders = {(r[0].shop_id, r[0].order_date) for r in rows}
    week_revenue = sum(float(r[2].price) * r[1].qty for r in rows)

    product_totals = defaultdict(int)
    for _, item, product in rows:
        product_totals[product.name] += item.qty
    top_products = sorted(product_totals.items(), key=lambda x: -x[1])[:5]

    lines = [
        "📊 <b>Статистика</b>\n",
        f"Сегодня: {len(today_shops)} кофеен, {today_qty} позиций, ~{today_revenue:.0f}₽",
        f"За 7 дней: {len(week_orders)} заказов, ~{week_revenue:.0f}₽",
        "",
        "Топ позиций за неделю:",
    ]
    for name, qty in top_products:
        lines.append(f"• {name}: {qty}")

    await message.answer("\n".join(lines), reply_markup=admin_menu_kb())


# ============================== ЗАПУСК ==============================

async def setup_commands(bot: Bot):
    """Настраивает системное меню команд Telegram ('/') — своё для кофеен, своё для админов."""
    shop_commands = [
        BotCommand(command="order", description="Собрать/изменить/отменить заказ"),
        BotCommand(command="status", description="Что уже в заказе"),
        BotCommand(command="help", description="Список команд"),
    ]
    await bot.set_my_commands(shop_commands, scope=BotCommandScopeDefault())

    admin_commands = [
        BotCommand(command="orders", description="Заказы на сегодня — принять/отменить"),
        BotCommand(command="report", description="Сводный отчёт на сегодня"),
        BotCommand(command="shops", description="Список кофеен"),
        BotCommand(command="catalog", description="Каталог товаров"),
        BotCommand(command="add_product", description="Добавить позицию: Название; ед.; цена"),
        BotCommand(command="approve", description="Подтвердить кофейню по id"),
        BotCommand(command="block", description="Заблокировать кофейню по id"),
        BotCommand(command="stats", description="Статистика за сегодня/неделю"),
        BotCommand(command="help", description="Список команд"),
    ]
    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logging.warning(f"Не удалось настроить меню команд для админа {admin_id}: {e}")


async def main():
    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(admin_router)  # раньше shop-роутера, чтобы callback'и админа не перехватывались
    dp.include_router(shop_router)

    await setup_commands(bot)

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        lock_and_report,
        trigger=CronTrigger(hour=CUTOFF_HOUR, minute=CUTOFF_MINUTE, timezone=TZ),
        args=[bot, ADMIN_CHAT_ID],
        id="daily_cutoff",
        replace_existing=True,
    )
    if REMINDER_BEFORE_MINUTES > 0:
        scheduler.add_job(
            remind_unordered_shops,
            trigger=CronTrigger(hour=REMINDER_HOUR, minute=REMINDER_MINUTE, timezone=TZ),
            args=[bot],
            id="daily_reminder",
            replace_existing=True,
        )
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
