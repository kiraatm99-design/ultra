import logging
import os
import json
import hashlib
import requests
from datetime import datetime, timedelta
from groq import Groq
from tavily import TavilyClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                           CallbackQueryHandler, filters, ContextTypes)
from flask import Flask
from threading import Thread

# ============================================================
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY", "5d44806d63094fdab0090cc5faef770c")
TAVILY_API_KEY   = os.environ.get("TAVILY_API_KEY", "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")
CHANNEL          = "@dasi_bet"
ADMIN_ID         = 7046072164
FREE_LIMIT       = 3
REFERRAL_GOAL    = 5
DB_FILE          = "users.json"
CACHE_FILE       = "cache.json"
CACHE_TTL_HOURS  = 6
BOT_USERNAME     = "football_analysist2_bot"
SEASON           = "2025"
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
groq_client   = Groq(api_key=GROQ_API_KEY)
tavily_client = TavilyClient(api_key=TAVILY_API_KEY)

LEAGUE_IDS = {
    "league_epl":        {"id": 2021, "name": "🏴󠁧󠁢󠁥󠁮󠁧󠁿 الدوري الإنجليزي"},
    "league_laliga":     {"id": 2014, "name": "🇪🇸 الدوري الإسباني"},
    "league_bundesliga": {"id": 2002, "name": "🇩🇪 الدوري الألماني"},
    "league_seriea":     {"id": 2019, "name": "🇮🇹 الدوري الإيطالي"},
    "league_ligue1":     {"id": 2015, "name": "🇫🇷 الدوري الفرنسي"},
    "league_ucl":        {"id": 2001, "name": "🌍 دوري الأبطال"},
    "league_saudi":      {"id": 2082, "name": "🇸🇦 الدوري السعودي"},
}

# ======= Flask Keep-Alive =======
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "✅ Bot is running!", 200

def keep_alive():
    Thread(target=lambda: flask_app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ======= Cache =======
def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def get_cache_key(text):
    return hashlib.md5(text.strip().lower().encode()).hexdigest()

def get_cached(key):
    cache = load_cache()
    if key in cache:
        entry = cache[key]
        age = datetime.now() - datetime.fromisoformat(entry["timestamp"])
        if age.total_seconds() < CACHE_TTL_HOURS * 3600:
            return entry["data"]
        del cache[key]
        save_cache(cache)
    return None

def set_cache(key, data):
    cache = load_cache()
    cache[key] = {"data": data, "timestamp": datetime.now().isoformat()}
    save_cache(cache)

# ======= Database =======
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r') as f:
            return json.load(f)
    return {"users": {}, "total_requests": 0}

def save_db(db):
    with open(DB_FILE, 'w') as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def get_user(db, user_id, update=None):
    uid = str(user_id)
    if uid not in db["users"]:
        db["users"][uid] = {
            "name": update.effective_user.full_name if update else "",
            "username": update.effective_user.username if update else "",
            "joined": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "requests_today": 0,
            "bonus_requests": 0,
            "last_request_date": "",
            "total_requests": 0,
            "vip": False,
            "vip_expiry": "",
            "blocked": False,
            "history": [],
            "ratings": [],
            "points": 0,
            "referrals": [],
            "referred_by": "",
        }
        save_db(db)
    return db["users"][uid]

def is_vip(db, user_id):
    if user_id == ADMIN_ID:
        return True
    user = get_user(db, user_id)
    if not user["vip"]:
        return False
    if user["vip_expiry"] and datetime.now().strftime("%Y-%m-%d") > user["vip_expiry"]:
        user["vip"] = False
        save_db(db)
        return False
    return True

def get_limit(db, user_id):
    if user_id == ADMIN_ID or is_vip(db, user_id):
        return 9999
    user = get_user(db, user_id)
    return FREE_LIMIT + user.get("bonus_requests", 0)

def check_daily_limit(db, user_id):
    if user_id == ADMIN_ID or is_vip(db, user_id):
        return True
    user = get_user(db, user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    if user["last_request_date"] != today:
        user["requests_today"] = 0
        user["last_request_date"] = today
        save_db(db)
    return user["requests_today"] < get_limit(db, user_id)

def remaining_requests(db, user_id):
    if user_id == ADMIN_ID or is_vip(db, user_id):
        return "♾️"
    user = get_user(db, user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    if user["last_request_date"] != today:
        return get_limit(db, user_id)
    return max(0, get_limit(db, user_id) - user["requests_today"])

def add_points(db, user_id, pts):
    user = get_user(db, user_id)
    user["points"] = user.get("points", 0) + pts
    if user["points"] >= 100:
        user["points"] -= 100
        expiry = datetime.now() + timedelta(days=1)
        user["vip"] = True
        user["vip_expiry"] = expiry.strftime("%Y-%m-%d")
        save_db(db)
        return True
    save_db(db)
    return False

def increment_requests(db, user_id, match):
    user = get_user(db, user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    if user["last_request_date"] != today:
        user["requests_today"] = 0
        user["last_request_date"] = today
    user["requests_today"] += 1
    user["total_requests"] += 1
    db["total_requests"] = db.get("total_requests", 0) + 1
    user.setdefault("history", []).append({"match": match, "date": datetime.now().strftime("%Y-%m-%d %H:%M")})
    if len(user["history"]) > 20:
        user["history"] = user["history"][-20:]
    add_points(db, user_id, 5)
    save_db(db)

def handle_referral(db, new_user_id, referrer_id):
    if str(new_user_id) == str(referrer_id):
        return
    referrer = get_user(db, referrer_id)
    if str(new_user_id) in referrer.get("referrals", []):
        return
    referrer.setdefault("referrals", []).append(str(new_user_id))
    get_user(db, new_user_id)["referred_by"] = str(referrer_id)
    if len(referrer["referrals"]) % REFERRAL_GOAL == 0:
        referrer["bonus_requests"] = referrer.get("bonus_requests", 0) + 1
    add_points(db, referrer_id, 10)
    save_db(db)

def is_arabic(text):
    return sum(1 for c in text if '\u0600' <= c <= '\u06FF') > len(text) * 0.2

# ======= Football API =======
def get_today_matches(league_id):
    cache_key = f"matches_{league_id}_{datetime.now().strftime('%Y-%m-%d')}"
    cached = get_cached(cache_key)
    if cached:
        return cached
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        url = f"https://api.football-data.org/v4/competitions/{league_id}/matches"
        headers = {"X-Auth-Token": FOOTBALL_API_KEY}
        params = {"dateFrom": today, "dateTo": today, "season": SEASON}
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            matches = r.json().get("matches", [])
            result = []
            for m in matches:
                home = m["homeTeam"]["name"]
                away = m["awayTeam"]["name"]
                time = m["utcDate"][11:16]
                result.append({"home": home, "away": away, "time": time, "id": m["id"]})
            set_cache(cache_key, result)
            return result
    except Exception as e:
        logger.error(f"Football API error: {e}")
    return []

def get_team_stats(team_name, league_id):
    cache_key = f"stats_{team_name}_{league_id}"
    cached = get_cached(cache_key)
    if cached:
        return cached
    try:
        url = f"https://api.football-data.org/v4/competitions/{league_id}/standings"
        headers = {"X-Auth-Token": FOOTBALL_API_KEY}
        params = {"season": SEASON}
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            standings = r.json().get("standings", [{}])[0].get("table", [])
            for team in standings:
                if team_name.lower() in team["team"]["name"].lower():
                    stats = {
                        "position": team["position"],
                        "points": team["points"],
                        "won": team["won"],
                        "draw": team["draw"],
                        "lost": team["lost"],
                        "goalsFor": team["goalsFor"],
                        "goalsAgainst": team["goalsAgainst"],
                    }
                    set_cache(cache_key, stats)
                    return stats
    except Exception as e:
        logger.error(f"Stats error: {e}")
    return {}

# ======= Tavily News =======
def get_team_news(team1, team2):
    cache_key = f"news_{get_cache_key(team1+team2)}"
    cached = get_cached(cache_key)
    if cached:
        return cached
    try:
        results = tavily_client.search(
            query=f"{team1} vs {team2} 2025 2026 injuries news preview",
            search_depth="basic",
            max_results=3
        )
        news = " | ".join([r.get("content", "")[:200] for r in results.get("results", [])])
        set_cache(cache_key, news)
        return news
    except Exception as e:
        logger.error(f"Tavily error: {e}")
    return ""

# ======= Prompts =======
ANALYSIS_PROMPT = """أنت محلل كرة قدم محترف. حلل مباراة موسم 2025/2026 فقط.

قاعدة اللغة: رد بنفس لغة المستخدم.

قدم تحليلاً مختصراً واحترافياً:

━━━━━━━━━━━━━━━━━━━━━
⚽ **{home} vs {away}**
━━━━━━━━━━━━━━━━━━━━━

📊 **الشكل الأخير:**
• {home}: [آخر 5 نتائج]
• {away}: [آخر 5 نتائج]

📰 **آخر الأخبار:**
{news}

📈 **الإحصائيات:**
{stats}

🏆 **التوقع:**
• الفائز المرجح: **[الفريق أو تعادل]**
• النتيجة الأرجح: **[X-X]**

🎯 **أفضل رهان:**
• التوقع: **[اسم الرهان]**
• 💰 الأود المقترح: **[X.XX]**
• 📊 نسبة الثقة: **[X]%**

⚠️ للترفيه فقط
━━━━━━━━━━━━━━━━━━━━━"""

SAFE_BET_PROMPT = """أنت محلل كرة قدم. بناءً على مباريات اليوم، اختر الرهان الأكثر أماناً وضماناً.

المباريات المتاحة اليوم:
{matches}

اختر مباراة واحدة فقط — الأكثر أماناً:

━━━━━━━━━━━━━━━━━━━━━
🔒 **الرهان الآمن لليوم**
━━━━━━━━━━━━━━━━━━━━━
⚽ المباراة: **[الفريق1 vs الفريق2]**
🎯 التوقع: **[التوقع]**
💰 الأود: **[X.XX]**
📊 نسبة الأمان: **[X]%**
💡 السبب: [سبب مختصر]
━━━━━━━━━━━━━━━━━━━━━
⚠️ للترفيه فقط"""

COUPON_PROMPT = """أنت محلل كرة قدم. اختر أفضل 4 توقعات آمنة من المباريات وضعها في قسيمة.

المباريات:
{matches}

موسم 2025/2026 فقط.

🎫 **القسيمة الذهبية**
━━━━━━━━━━━━━━━━━━━━━
[1]. [الفريق1 vs الفريق2]
   ✅ [التوقع] | 💰 [X.XX] | 📊 [X]%

[2]. [الفريق1 vs الفريق2]
   ✅ [التوقع] | 💰 [X.XX] | 📊 [X]%

[3]. [الفريق1 vs الفريق2]
   ✅ [التوقع] | 💰 [X.XX] | 📊 [X]%

[4]. [الفريق1 vs الفريق2]
   ✅ [التوقع] | 💰 [X.XX] | 📊 [X]%
━━━━━━━━━━━━━━━━━━━━━
💰 الأود الإجمالي: **[X.XX]**
📊 نسبة النجاح: **[X]%**
━━━━━━━━━━━━━━━━━━━━━
⚠️ للترفيه فقط"""

# ======= Keyboards =======
async def is_subscribed(user_id, context):
    if user_id == ADMIN_ID:
        return True
    try:
        member = await context.bot.get_chat_member(CHANNEL, user_id)
        return member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]
    except:
        return False

def subscribe_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 اشترك في القناة", url=f"https://t.me/{CHANNEL[1:]}")],
        [InlineKeyboardButton("✅ تحقق", callback_data="check_sub")]
    ])

def main_keyboard(user_id, db):
    vip = is_vip(db, user_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 الدوريات", callback_data="leagues"),
         InlineKeyboardButton("🔒 أأمن رهان", callback_data="safe_bet")],
        [InlineKeyboardButton("🎫 قسيمة ذهبية", callback_data="coupon"),
         InlineKeyboardButton("⚽ تحليل مباراة", callback_data="predict")],
        [InlineKeyboardButton("👥 أحل صديقاً", callback_data="referral"),
         InlineKeyboardButton("📊 إحصائياتي", callback_data="my_stats")],
        [InlineKeyboardButton("📜 سجلي", callback_data="history"),
         InlineKeyboardButton("💎 VIP $5/شهر", callback_data="vip_info")
         if not vip else InlineKeyboardButton("💎 VIP ✅", callback_data="my_stats")],
    ])

def leagues_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏴󠁧󠁢󠁥󠁮󠁧󠁿 الإنجليزي", callback_data="league_epl"),
         InlineKeyboardButton("🇪🇸 الإسباني", callback_data="league_laliga")],
        [InlineKeyboardButton("🇩🇪 الألماني", callback_data="league_bundesliga"),
         InlineKeyboardButton("🇮🇹 الإيطالي", callback_data="league_seriea")],
        [InlineKeyboardButton("🇫🇷 الفرنسي", callback_data="league_ligue1"),
         InlineKeyboardButton("🌍 دوري الأبطال", callback_data="league_ucl")],
        [InlineKeyboardButton("🇸🇦 السعودي", callback_data="league_saudi"),
         InlineKeyboardButton("🔙 رجوع", callback_data="back_main")],
    ])

def matches_keyboard(matches, league_key):
    buttons = []
    for i, m in enumerate(matches[:10]):
        label = f"⚽ {m['home']} vs {m['away']} - {m['time']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"match_{league_key}_{i}")])
    buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="leagues")])
    return InlineKeyboardMarkup(buttons)

def rating_keyboard(match_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("1⭐", callback_data=f"rate_1_{match_id}"),
        InlineKeyboardButton("2⭐", callback_data=f"rate_2_{match_id}"),
        InlineKeyboardButton("3⭐", callback_data=f"rate_3_{match_id}"),
        InlineKeyboardButton("4⭐", callback_data=f"rate_4_{match_id}"),
        InlineKeyboardButton("5⭐", callback_data=f"rate_5_{match_id}"),
    ]])

def vip_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 اشترك الآن $5/شهر", callback_data="pay_vip")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]
    ])

# ======= Core Analysis =======
async def do_analysis(home, away, league_id=None):
    cache_key = get_cache_key(f"{home}_vs_{away}")
    cached = get_cached(cache_key)
    if cached:
        return cached, True

    news = get_team_news(home, away)
    stats_home = get_team_stats(home, league_id) if league_id else {}
    stats_away = get_team_stats(away, league_id) if league_id else {}

    stats_text = ""
    if stats_home:
        stats_text += f"• {home}: المركز {stats_home.get('position','?')} | {stats_home.get('points','?')} نقطة | {stats_home.get('won','?')}ف {stats_home.get('draw','?')}ت {stats_home.get('lost','?')}خ\n"
    if stats_away:
        stats_text += f"• {away}: المركز {stats_away.get('position','?')} | {stats_away.get('points','?')} نقطة | {stats_away.get('won','?')}ف {stats_away.get('draw','?')}ت {stats_away.get('lost','?')}خ"
    if not stats_text:
        stats_text = "غير متاحة"

    prompt = ANALYSIS_PROMPT.format(home=home, away=away, news=news or "لا أخبار متاحة", stats=stats_text)

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=800,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"حلل مباراة {home} ضد {away} موسم 2025/2026"}
        ]
    )
    result = response.choices[0].message.content
    set_cache(cache_key, result)
    return result, False

# ======= Start =======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    user_id = update.effective_user.id
    get_user(db, user_id, update)

    if context.args and context.args[0].startswith("ref_"):
        handle_referral(db, user_id, context.args[0].replace("ref_", ""))

    if not await is_subscribed(user_id, context):
        await update.message.reply_text(
            "⛔ *يجب الاشتراك أولاً!*\n\n"
            f"📢 {CHANNEL}",
            parse_mode="Markdown",
            reply_markup=subscribe_keyboard()
        )
        return

    user = get_user(db, user_id)
    vip_badge = "💎 VIP" if is_vip(db, user_id) else "🆓 مجاني"
    remaining = remaining_requests(db, user_id)

    await update.message.reply_text(
        f"🤖 *بوت توقعات المباريات*\n\n"
        f"👤 {update.effective_user.first_name}\n"
        f"🏷️ {vip_badge} | 🎯 متبقي: *{remaining}* | ⭐ {user.get('points',0)}/100\n\n"
        f"اختر من القائمة 👇",
        parse_mode="Markdown",
        reply_markup=main_keyboard(user_id, db)
    )

# ======= Button Handler =======
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = load_db()
    user_id = query.from_user.id
    data = query.data

    # ---- تحقق اشتراك ----
    if data == "check_sub":
        if await is_subscribed(user_id, context):
            await query.edit_message_text("✅ تم! اضغط /start", parse_mode="Markdown")
        else:
            await query.edit_message_text(f"❌ اشترك في {CHANNEL} أولاً.", reply_markup=subscribe_keyboard())
        return

    # ---- الدوريات ----
    if data == "leagues":
        await query.edit_message_text("🏆 *اختر الدوري:*", parse_mode="Markdown", reply_markup=leagues_keyboard())
        return

    if data.startswith("league_") and not data.startswith("league_match"):
        league_info = LEAGUE_IDS.get(data)
        if not league_info:
            return
        await query.edit_message_text(f"⏳ جاري جلب مباريات {league_info['name']} اليوم...")
        matches = get_today_matches(league_info["id"])
        if not matches:
            await query.edit_message_text(
                f"😔 لا توجد مباريات لـ {league_info['name']} اليوم.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="leagues")]])
            )
            return
        context.user_data[f"matches_{data}"] = matches
        await query.edit_message_text(
            f"{league_info['name']}\n📅 مباريات اليوم — اختر مباراة للتحليل:",
            reply_markup=matches_keyboard(matches, data)
        )
        return

    # ---- تحليل مباراة من الدوري ----
    if data.startswith("match_"):
        parts = data.split("_", 2)
        league_key = "_".join(parts[1:-1]) if len(parts) > 2 else parts[1]
        idx = int(parts[-1])
        # استرجاع المباراة
        league_key_full = None
        for k in LEAGUE_IDS:
            if k in data:
                league_key_full = k
                break
        matches = context.user_data.get(f"matches_{league_key_full}", [])
        if not matches or idx >= len(matches):
            await query.edit_message_text("❌ المباراة غير متاحة، حاول من جديد.")
            return

        if not check_daily_limit(db, user_id):
            await query.edit_message_text(
                "⛔ انتهت توقعاتك اليوم!\n💎 اشترك VIP للمزيد.",
                reply_markup=vip_keyboard()
            )
            return

        m = matches[idx]
        home, away = m["home"], m["away"]
        league_id = LEAGUE_IDS.get(league_key_full, {}).get("id")

        await query.edit_message_text(f"🔍 جاري تحليل {home} vs {away}...")
        try:
            result, from_cache = await do_analysis(home, away, league_id)
            increment_requests(db, user_id, f"{home} vs {away}")
            remaining = remaining_requests(db, user_id)
            cache_note = "⚡ من الكاش" if from_cache else "🔍 تحليل جديد"
            try:
                await context.bot.send_message(
                    query.message.chat_id, result, parse_mode="Markdown"
                )
            except:
                await context.bot.send_message(query.message.chat_id, result)
            await context.bot.send_message(
                query.message.chat_id,
                f"{cache_note} | 🎯 متبقي: *{remaining}*\nقيّم التوقع:",
                parse_mode="Markdown",
                reply_markup=rating_keyboard(hash(f"{home}{away}") % 10000)
            )
        except Exception as e:
            logger.error(e)
            await context.bot.send_message(query.message.chat_id, "❌ حدث خطأ، حاول مرة أخرى.")
        return

    # ---- أأمن رهان ----
    if data == "safe_bet":
        await query.edit_message_text("🔍 جاري البحث عن أأمن رهان اليوم... ⏳")
        cache_key = f"safe_bet_{datetime.now().strftime('%Y-%m-%d')}"
        cached = get_cached(cache_key)
        if cached:
            try:
                await query.edit_message_text(cached + "\n\n⚡ من الكاش", parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]))
            except:
                await query.edit_message_text(cached + "\n\n⚡ من الكاش",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]))
            return

        all_matches = []
        for key, info in LEAGUE_IDS.items():
            matches = get_today_matches(info["id"])
            for m in matches:
                all_matches.append(f"{m['home']} vs {m['away']} ({info['name']})")

        if not all_matches:
            await query.edit_message_text(
                "😔 لا توجد مباريات اليوم.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]])
            )
            return

        matches_text = "\n".join(all_matches[:15])
        try:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=500,
                messages=[
                    {"role": "system", "content": SAFE_BET_PROMPT.format(matches=matches_text)},
                    {"role": "user", "content": "اختر أأمن رهان لليوم موسم 2025/2026"}
                ]
            )
            result = response.choices[0].message.content
            set_cache(cache_key, result)
            try:
                await query.edit_message_text(result, parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]))
            except:
                await query.edit_message_text(result,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]))
        except Exception as e:
            logger.error(e)
            await query.edit_message_text("❌ حدث خطأ، حاول لاحقاً.")
        return

    # ---- قسيمة ذهبية ----
    if data == "coupon":
        if not is_vip(db, user_id):
            await query.edit_message_text(
                "🔒 *القسيمة الذهبية للمشتركين VIP فقط!*\n\n💎 اشترك بـ $5/شهر",
                parse_mode="Markdown", reply_markup=vip_keyboard()
            )
            return
        await query.edit_message_text("⏳ جاري بناء القسيمة الذهبية...")
        all_matches = []
        for key, info in LEAGUE_IDS.items():
            matches = get_today_matches(info["id"])
            for m in matches:
                all_matches.append(f"{m['home']} vs {m['away']}")
        if not all_matches:
            await query.edit_message_text("😔 لا توجد مباريات اليوم لبناء القسيمة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]))
            return
        cache_key = f"coupon_{datetime.now().strftime('%Y-%m-%d')}"
        cached = get_cached(cache_key)
        if cached:
            try:
                await query.edit_message_text(cached, parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]))
            except:
                await query.edit_message_text(cached,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]))
            return
        matches_text = "\n".join(all_matches[:10])
        try:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=800,
                messages=[
                    {"role": "system", "content": COUPON_PROMPT.format(matches=matches_text)},
                    {"role": "user", "content": "اختر أفضل 4 مباريات لقسيمة موسم 2025/2026"}
                ]
            )
            result = response.choices[0].message.content
            set_cache(cache_key, result)
            try:
                await query.edit_message_text(result, parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]))
            except:
                await query.edit_message_text(result,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]))
        except Exception as e:
            logger.error(e)
            await query.edit_message_text("❌ حدث خطأ.")
        return

    # ---- تحليل مباراة يدوي ----
    if data == "predict":
        await query.edit_message_text(
            "⚽ *أرسل اسم المباراة:*\n\nمثال: ريال مدريد vs برشلونة",
            parse_mode="Markdown"
        )
        context.user_data["mode"] = "predict"
        return

    # ---- إحالة ----
    if data == "referral":
        user = get_user(db, user_id, query)
        ref_count = len(user.get("referrals", []))
        bonus = user.get("bonus_requests", 0)
        next_bonus = REFERRAL_GOAL - (ref_count % REFERRAL_GOAL)
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        await query.edit_message_text(
            f"👥 *نظام الإحالة*\n\n"
            f"🔗 رابطك:\n`{ref_link}`\n\n"
            f"• إجمالي إحالاتك: *{ref_count}*\n"
            f"• توقعات مكسوبة: *{bonus}*\n"
            f"• تحتاج *{next_bonus}* إحالة للتوقع القادم\n\n"
            f"كل {REFERRAL_GOAL} إحالات = توقع مجاني إضافي! 🎁",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 شارك", switch_inline_query=f"انضم لبوت التوقعات! {ref_link}")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]
            ])
        )
        return

    # ---- السجل ----
    if data == "history":
        user = get_user(db, user_id, query)
        history = user.get("history", [])
        if not history:
            await query.edit_message_text("📜 لا يوجد سجل بعد!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]))
        else:
            text = "📜 *آخر توقعاتك:*\n\n"
            for i, h in enumerate(reversed(history[-10:]), 1):
                text += f"{i}. ⚽ {h['match']} — {h['date']}\n"
            await query.edit_message_text(text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]))
        return

    # ---- إحصائيات ----
    if data == "my_stats":
        user = get_user(db, user_id, query)
        vip_badge = "💎 VIP" if is_vip(db, user_id) else "🆓 مجاني"
        await query.edit_message_text(
            f"📊 *إحصائياتك:*\n\n"
            f"👤 {user['name']}\n"
            f"🏷️ {vip_badge}\n"
            f"🎯 متبقي اليوم: {remaining_requests(db, user_id)}\n"
            f"📈 إجمالي طلباتك: {user['total_requests']}\n"
            f"👥 إحالاتك: {len(user.get('referrals',[]))}\n"
            f"⭐ نقاطك: {user.get('points',0)}/100\n"
            f"🎁 توقعات مكسوبة: {user.get('bonus_requests',0)}\n"
            f"📅 انضمت: {user['joined']}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="back_main")]]))
        return

    # ---- VIP ----
    if data == "vip_info":
        await query.edit_message_text(
            "💎 *اشتراك VIP - $5/شهر*\n\n"
            "✅ توقعات غير محدودة\n"
            "✅ القسيمة الذهبية اليومية\n"
            "✅ أأمن رهان يومي\n"
            "✅ تحليل احترافي بالأودد\n\n"
            "تواصل مع المشرف للاشتراك 👇",
            parse_mode="Markdown", reply_markup=vip_keyboard()
        )
        return

    if data == "pay_vip":
        await query.edit_message_text(
            "💳 *للاشتراك:*\n\n👤 @Admin\n💰 $5/شهر\n\nطرق الدفع:\n• USDT\n• PayPal\n• تحويل بنكي",
            parse_mode="Markdown"
        )
        return

    if data == "back_main":
        user = get_user(db, user_id)
        vip_badge = "💎 VIP" if is_vip(db, user_id) else "🆓 مجاني"
        await query.edit_message_text(
            f"🤖 *بوت توقعات المباريات*\n\n"
            f"🏷️ {vip_badge} | 🎯 متبقي: *{remaining_requests(db, user_id)}* | ⭐ {user.get('points',0)}/100\n\n"
            f"اختر من القائمة 👇",
            parse_mode="Markdown",
            reply_markup=main_keyboard(user_id, db)
        )
        return

    # ---- تقييم ----
    if data.startswith("rate_"):
        parts = data.split("_")
        stars = int(parts[1])
        user = get_user(db, user_id, query)
        user.setdefault("ratings", []).append({"stars": stars, "date": datetime.now().strftime("%Y-%m-%d")})
        add_points(db, user_id, stars)
        await query.edit_message_text(f"{'⭐'*stars} شكراً! +{stars} نقاط 🎉")
        return

# ======= Text Analysis =======
async def analyze_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if not await is_subscribed(user_id, context):
        await update.message.reply_text(f"⛔ يجب الاشتراك في {CHANNEL} أولاً!", reply_markup=subscribe_keyboard())
        return

    user = get_user(db, user_id, update)
    if user.get("blocked"):
        await update.message.reply_text("⛔ تم حظرك.")
        return

    if not check_daily_limit(db, user_id):
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        await update.message.reply_text(
            "⛔ *انتهت توقعاتك اليوم!*\n\n"
            f"🆓 شارك رابطك وكل {REFERRAL_GOAL} أصدقاء = توقع مجاني!\n`{ref_link}`\n\n"
            "💎 أو اشترك VIP للتوقعات غير المحدودة",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 VIP", callback_data="vip_info")],
                [InlineKeyboardButton("👥 رابط الإحالة", callback_data="referral")]
            ])
        )
        return

    wait = await update.message.reply_text("🔍 جاري التحليل... ⏳")
    try:
        home, away = text, text
        if " vs " in text.lower():
            parts = text.lower().split(" vs ")
            home, away = text.split(" vs ")[0].strip(), text.split(" vs ")[1].strip()
        elif " ضد " in text:
            home, away = text.split(" ضد ")[0].strip(), text.split(" ضد ")[1].strip()

        result, from_cache = await do_analysis(home, away)
        increment_requests(db, user_id, f"{home} vs {away}")
        remaining = remaining_requests(db, user_id)
        cache_note = "⚡ من الكاش" if from_cache else "🔍 تحليل جديد"

        await wait.delete()
        try:
            await update.message.reply_text(result, parse_mode="Markdown")
        except:
            await update.message.reply_text(result)

        await update.message.reply_text(
            f"{cache_note} | 🎯 متبقي: *{remaining}* | ⭐ {user.get('points',0)}/100\nقيّم:",
            parse_mode="Markdown",
            reply_markup=rating_keyboard(hash(text) % 10000)
        )
    except Exception as e:
        logger.error(e)
        await wait.edit_text("❌ حدث خطأ، حاول مرة أخرى.")

# ======= Admin =======
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    db = load_db()
    total = len(db["users"])
    vip_c = sum(1 for u in db["users"].values() if u.get("vip"))
    today = datetime.now().strftime("%Y-%m-%d")
    active = sum(1 for u in db["users"].values() if u.get("last_request_date") == today)
    cache = load_cache()
    await update.message.reply_text(
        f"👑 *لوحة التحكم*\n\n"
        f"👥 المستخدمون: {total} | 💎 VIP: {vip_c}\n"
        f"🟢 نشطون اليوم: {active}\n"
        f"📊 الطلبات: {db.get('total_requests',0)}\n"
        f"💾 الكاش: {len(cache)} مدخل\n\n"
        f"/vip [ID] | /unvip [ID]\n"
        f"/ban [ID] | /unban [ID]\n"
        f"/broadcast [رسالة]\n"
        f"/users | /stats | /clearcache",
        parse_mode="Markdown"
    )

async def vip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not context.args:
        return
    db = load_db()
    uid = str(context.args[0])
    if uid in db["users"]:
        db["users"][uid]["vip"] = True
        expiry = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        db["users"][uid]["vip_expiry"] = expiry
        save_db(db)
        await update.message.reply_text(f"✅ VIP مفعّل لـ {uid} حتى {expiry}")
        try:
            await context.bot.send_message(int(uid), "🎉 *تم تفعيل VIP!*\n\nاضغط /start 🚀", parse_mode="Markdown")
        except:
            pass
    else:
        await update.message.reply_text("❌ المستخدم غير موجود")

async def unvip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not context.args:
        return
    db = load_db()
    uid = str(context.args[0])
    if uid in db["users"]:
        db["users"][uid]["vip"] = False
        save_db(db)
        await update.message.reply_text(f"✅ إلغاء VIP لـ {uid}")

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not context.args:
        return
    db = load_db()
    uid = str(context.args[0])
    if uid in db["users"]:
        db["users"][uid]["blocked"] = True
        save_db(db)
        await update.message.reply_text(f"⛔ حظر {uid}")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not context.args:
        return
    db = load_db()
    uid = str(context.args[0])
    if uid in db["users"]:
        db["users"][uid]["blocked"] = False
        save_db(db)
        await update.message.reply_text(f"✅ فك حظر {uid}")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not context.args:
        return
    db = load_db()
    msg = " ".join(context.args)
    sent = failed = 0
    for uid in db["users"]:
        try:
            await context.bot.send_message(int(uid), f"📢 *رسالة:*\n\n{msg}", parse_mode="Markdown")
            sent += 1
        except:
            failed += 1
    await update.message.reply_text(f"✅ {sent} | ❌ {failed}")

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    db = load_db()
    text = "👥 *المستخدمون:*\n\n"
    for uid, u in list(db["users"].items())[-20:]:
        v = "💎" if u.get("vip") else "🆓"
        b = "⛔" if u.get("blocked") else ""
        text += f"{v}{b} `{uid}` {u.get('name','?')} | {u.get('total_requests',0)}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    db = load_db()
    today = datetime.now().strftime("%Y-%m-%d")
    active = sum(1 for u in db["users"].values() if u.get("last_request_date") == today)
    await update.message.reply_text(
        f"📊 *إحصائيات:*\n\n"
        f"👥 {len(db['users'])} مستخدم\n"
        f"💎 {sum(1 for u in db['users'].values() if u.get('vip'))} VIP\n"
        f"🟢 {active} نشط اليوم\n"
        f"📈 {db.get('total_requests',0)} طلب إجمالي",
        parse_mode="Markdown"
    )

async def clearcache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    save_cache({})
    await update.message.reply_text("✅ تم مسح الكاش!")

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    today = datetime.now().strftime("%Y-%m-%d")
    active = sum(1 for u in db["users"].values() if u.get("last_request_date") == today)
    try:
        await context.bot.send_message(ADMIN_ID,
            f"📊 *التقرير اليومي - {today}*\n\n"
            f"👥 {len(db['users'])} مستخدم\n"
            f"🟢 {active} نشط اليوم\n"
            f"📈 {db.get('total_requests',0)} طلب إجمالي",
            parse_mode="Markdown")
    except:
        pass

# ======= Main =======
def main():
    keep_alive()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.job_queue.run_daily(send_daily_report, time=datetime.strptime("08:00", "%H:%M").time())

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("vip", vip_command))
    app.add_handler(CommandHandler("unvip", unvip_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("clearcache", clearcache_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_match))

    logger.info("✅ البوت يعمل!")
    app.run_polling()

if __name__ == "__main__":
    main()
