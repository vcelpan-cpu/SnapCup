import asyncio
import enum
import logging
import os
from collections import defaultdict
from datetime import datetime, date
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Enum, ForeignKey, Integer,
    Numeric, String, UniqueConstraint, func, select
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

# Чат/группа кухни, куда падают отчёты и заявки на подтверждение новых кофеен
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])

# ID телеграм-аккаунтов, которые могут управлять каталогом и подтверждать кофейни
ADMIN_USER_IDS = {int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()}

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))

# Время, после которого приём заказов на сегодня закрывается и уходит отчёт на кухню
CUTOFF_HOUR = int(os.environ.get("CUTOFF_HOUR", 15))
CUTOFF_MINUTE = int(os.environ.get("CUTOFF_MINUTE", 0))


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
    draft = "draft"
    submitted = "submitted"
    locked = "locked"


class CoffeeShop(Base):
    __tablename__ = "coffee_shops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
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
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.draft)
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
        order = Order(shop_id=shop_id, order_date=today(), status=OrderStatus.draft)
        session.add(order)
        await session.flush()
    return order


# ============================== ХЕНДЛЕРЫ: КОФЕЙНИ ==============================

shop_router = Router()


class Registration(StatesGroup):
    waiting_name = State()


@shop_router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    shop = await get_shop(message.from_user.id)
    if shop is None:
        await state.set_state(Registration.waiting_name)
        await message.answer(
            "👋 Привет! Это бот приёма заказов для кондитерской.\n\n"
            "Как называется ваша кофейня? Напишите название одним сообщением — "
            "заявку отправлю на подтверждение."
        )
        return
    if shop.status == ShopStatus.pending:
        await message.answer("⏳ Заявка на подключение ещё на рассмотрении. Мы сообщим, как только подтвердят.")
        return
    if shop.status == ShopStatus.blocked:
        await message.answer("🚫 Доступ к боту для вашей точки закрыт. Свяжитесь с кондитерской напрямую.")
        return
    await message.answer(
        f"С возвращением, {shop.name}! 🧁\n"
        "Команды:\n"
        "/order — собрать заказ на сегодня\n"
        "/status — что уже в заказе"
    )


@shop_router.message(Registration.waiting_name)
async def save_shop_name(message: Message, state: FSMContext, bot: Bot):
    name = message.text.strip()[:128]
    async with await get_session() as session:
        shop = CoffeeShop(tg_id=message.from_user.id, name=name, status=ShopStatus.pending)
        session.add(shop)
        await session.commit()
        await session.refresh(shop)

    await state.clear()
    await message.answer("✅ Заявка отправлена. Ждите подтверждения — я напишу сразу, как одобрят.")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"shop_approve:{shop.id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"shop_reject:{shop.id}"),
    ]])
    await bot.send_message(
        ADMIN_CHAT_ID,
        f"🆕 Новая заявка от кофейни: <b>{name}</b>\nTG ID: {message.from_user.id}",
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
    return InlineKeyboardMarkup(inline_keyboard=rows)


@shop_router.message(Command("order"))
async def cmd_order(message: Message):
    shop = await get_shop(message.from_user.id)
    if shop is None or shop.status != ShopStatus.approved:
        await message.answer("Сначала нужно подтверждение доступа. Отправьте /start.")
        return

    async with await get_session() as session:
        order = await get_or_create_draft(session, shop.id)
        if order.status == OrderStatus.locked:
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
        await session.commit()

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
        if order.status == OrderStatus.locked:
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
async def submit_order(callback: CallbackQuery):
    shop = await get_shop(callback.from_user.id)
    async with await get_session() as session:
        order = await get_or_create_draft(session, shop.id)
        items = (await session.execute(
            select(OrderItem).where(OrderItem.order_id == order.id, OrderItem.qty > 0)
        )).scalars().all()
        if not items:
            await callback.answer("Заказ пустой — добавьте хотя бы одну позицию", show_alert=True)
            return
        order.status = OrderStatus.submitted
        await session.commit()

    await callback.message.answer(
        "✅ Заказ оформлен и передан в очередь на кухню.\n"
        "До кат-оффа его ещё можно поменять командой /order."
    )
    await callback.answer()


@shop_router.message(Command("status"))
async def cmd_status(message: Message):
    shop = await get_shop(message.from_user.id)
    if shop is None or shop.status != ShopStatus.approved:
        await message.answer("Сначала нужно подтверждение доступа. Отправьте /start.")
        return

    async with await get_session() as session:
        order = await get_or_create_draft(session, shop.id)
        items = (await session.execute(
            select(OrderItem, Product).join(Product).where(
                OrderItem.order_id == order.id, OrderItem.qty > 0
            )
        )).all()

    if not items:
        await message.answer("На сегодня заказ пока пустой. /order — чтобы собрать.")
        return

    lines = [f"• {p.name}: {i.qty} {p.unit}" for i, p in items]
    status_label = {"draft": "черновик", "submitted": "оформлен", "locked": "закрыт, на кухне"}[order.status.value]
    await message.answer(f"📋 Заказ на сегодня ({status_label}):\n" + "\n".join(lines))


# ============================== ХЕНДЛЕРЫ: АДМИН ==============================

admin_router = Router()


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
    await bot.send_message(tg_id, "🎉 Заявка подтверждена! Команда /order — оформить заказ на сегодня.")
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


@admin_router.message(Command("shops"))
async def list_shops(message: Message):
    if not is_admin(message.from_user.id):
        return
    async with await get_session() as session:
        shops = (await session.execute(select(CoffeeShop))).scalars().all()
    if not shops:
        await message.answer("Кофеен пока нет.")
        return
    labels = {"pending": "⏳", "approved": "✅", "blocked": "🚫"}
    lines = [f"{labels[s.status.value]} #{s.id} {s.name}" for s in shops]
    await message.answer("\n".join(lines))


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


@admin_router.message(Command("add_product"))
async def add_product(message: Message):
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


@admin_router.message(Command("catalog"))
async def show_catalog(message: Message):
    if not is_admin(message.from_user.id):
        return
    async with await get_session() as session:
        products = (await session.execute(select(Product).order_by(Product.sort_order))).scalars().all()
    if not products:
        await message.answer("Каталог пуст. /add_product Название; ед.; цена")
        return
    lines = [f"{'✅' if p.active else '🚫'} #{p.id} {p.name} — {p.price}₽/{p.unit}" for p in products]
    lines.append("\n/toggle_product <id> — включить/выключить позицию")
    await message.answer("\n".join(lines))


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


async def build_report_text() -> str | None:
    async with await get_session() as session:
        rows = (await session.execute(
            select(Order, CoffeeShop, OrderItem, Product)
            .join(CoffeeShop, Order.shop_id == CoffeeShop.id)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .join(Product, OrderItem.product_id == Product.id)
            .where(Order.order_date == today(), OrderItem.qty > 0)
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


async def lock_and_report(bot: Bot, admin_chat_id: int):
    async with await get_session() as session:
        orders = (await session.execute(
            select(Order).where(Order.order_date == today(), Order.status != OrderStatus.locked)
        )).scalars().all()
        for order in orders:
            order.status = OrderStatus.locked
        await session.commit()

    text = await build_report_text()
    if text is None:
        text = f"📦 Заказов на {today().strftime('%d.%m.%Y')} нет."
    await bot.send_message(admin_chat_id, text)


@admin_router.message(Command("report"))
async def manual_report(message: Message):
    if not is_admin(message.from_user.id):
        return
    text = await build_report_text()
    await message.answer(text or "Заказов на сегодня пока нет.")


# ============================== ЗАПУСК ==============================

async def main():
    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(admin_router)  # раньше shop-роутера, чтобы callback'и админа не перехватывались
    dp.include_router(shop_router)

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        lock_and_report,
        trigger=CronTrigger(hour=CUTOFF_HOUR, minute=CUTOFF_MINUTE, timezone=TZ),
        args=[bot, ADMIN_CHAT_ID],
        id="daily_cutoff",
        replace_existing=True,
    )
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
