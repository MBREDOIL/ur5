import aiohttp
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    JobQueue
)
import logging
import tempfile
import os
from datetime import datetime, timedelta
import asyncio

from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Dummy server for health check
def start_dummy_server():
    class SimpleHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Telegram bot is running.')

    server = HTTPServer(('0.0.0.0', 8080), SimpleHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

BASE_URL = "http://14.139.56.104"
DEFAULT_TIMEOUT = 80
DEFAULT_ITEMS = 10
MIN_TIMEOUT = 8
MAX_TIMEOUT = 300
MIN_ITEMS = 10
MAX_ITEMS = 80
CACHE_TIME = timedelta(minutes=15)
SESSION_TIMEOUT = 300  # 5 minutes
SESSION_DURATION = 900  # 15 minutes in seconds

# Updated User Agents
USER_AGENTS = [
    # Chrome (Windows/Mac)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    
    # Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    
    # Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    
    # Mobile
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S911B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36"
]

class ResultCache:
    def __init__(self):
        self.results = []
        self.timestamp = datetime.min
        self.active_sessions = {}

cache = ResultCache()

async def get_headers():
    """Generate headers with random user agent"""
    return {
        "User-Agent": USER_AGENTS[datetime.now().second % len(USER_AGENTS)],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache"
    }


async def fetch_results(session, timeout):
    """Stream and parse results with early return"""
    try:
        if datetime.now() - cache.timestamp < CACHE_TIME:
            return cache.results

        cache.results = []
        buffer = ""
        async with session.get(BASE_URL, headers=await get_headers(), timeout=timeout) as response:
            async for chunk in response.content.iter_chunked(1024):
                buffer += chunk.decode('utf-8', errors='ignore')
                
                while True:
                    table_start = buffer.find('<table id="ResultList"')
                    if table_start == -1:
                        break
                    
                    table_end = buffer.find('</table>', table_start)
                    if table_end == -1:
                        break
                        
                    table_html = buffer[table_start:table_end+8]
                    soup = BeautifulSoup(table_html, 'html.parser')
                    
                    new_results = []
                    for row in soup.find_all('tr'):
                        cols = row.find_all('td')
                        if len(cols) == 2 and (link := cols[1].find('a')):
                            new_results.append({
                                'name': re.sub(r'\s+', ' ', link.text.strip()),
                                'url': BASE_URL + link['href'] if not link['href'].startswith('http') else link['href']
                            })
                    
                    cache.results.extend(new_results)
                    buffer = buffer[table_end+8:]
                    
                    # Return early when minimum results reached
                    if len(cache.results) >= MIN_RESULTS_TO_SHOW:
                        cache.timestamp = datetime.now()
                        return cache.results

        cache.timestamp = datetime.now()
        return cache.results

    except Exception as e:
        logger.error(f"Fetch error: {e}")
        return []
                        

#async def fetch_results(session, timeout):
    """Fetch results with caching and retry logic"""
    try:
        if datetime.now() - cache.timestamp < CACHE_TIME:
            return cache.results
            
        async with session.get(BASE_URL, headers=await get_headers(), timeout=timeout) as response:
            if response.status == 429:
                logger.warning("Rate limited - waiting 5 seconds")
                await asyncio.sleep(5)
                return await fetch_results(session, timeout)
                
            text = await response.text()
            soup = BeautifulSoup(text, 'html.parser')
            
            results = []
            table = soup.find('table', {'id': 'ResultList'})
            if table:
                for row in table.find_all('tr'):
                    cols = row.find_all('td')
                    if len(cols) == 2:
                        link = cols[1].find('a')
                        if link:
                            results.append({
                                'name': link.text.strip(),
                                'url': BASE_URL + link['href'] if not link['href'].startswith('http') else link['href']
                            })
            
            cache.results = results
            cache.timestamp = datetime.now()
            return results
            
    except Exception as e:
        logger.error(f"Fetch error: {e}")
        return []

def generate_keyboard(results, page, items_per_page):
    """Generate dynamic paginated keyboard"""
    items_per_page = max(MIN_ITEMS, min(MAX_ITEMS, items_per_page))
    start = page * items_per_page
    page_results = results[start:start+items_per_page]
    
    buttons = [
        [InlineKeyboardButton(f"{start+i+1}. {res['name'][:30]}", callback_data=f"result_{start+i}")]
        for i, res in enumerate(page_results)
    ]
    
    # Navigation controls
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Back", callback_data="page_back"))
    if start+items_per_page < len(results):
        nav_buttons.append(InlineKeyboardButton("▶️ Next", callback_data="page_next"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    buttons.append([InlineKeyboardButton("❌ Cancel Session", callback_data="main_cancel")])
    navigation.append(InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu"))
    
    if navigation:
        keyboard.append(navigation)
    
    return InlineKeyboardMarkup(buttons)

# Keep other handlers (settings, help, etc) similar to original

#def generate_keyboard(results, page, items_per_page):
    """Generate paginated keyboard"""
    start = page * items_per_page
    end = start + items_per_page
    page_results = results[start:end]
    
    keyboard = [
        [InlineKeyboardButton(f"{start + idx + 1}. {res['name']}", callback_data=f"result_{start + idx}")]
        for idx, res in enumerate(page_results)
    ]
    
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("◀️ Back", callback_data="page_back"))
    if end < len(results):
        navigation.append(InlineKeyboardButton("▶️ Next", callback_data="page_next"))
    navigation.append(InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu"))
    
    if navigation:
        keyboard.append(navigation)
    
    return InlineKeyboardMarkup(keyboard)

async def main_menu(update: Update):
    keyboard = [
        [InlineKeyboardButton("📊 Check Results", callback_data="main_results")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="main_settings")],
        [InlineKeyboardButton("❓ Help Guide", callback_data="main_help")],
        [InlineKeyboardButton("❌ Cancel Session", callback_data="main_cancel")]
    ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.message:
        await update.message.reply_text(
            "🏫 University of Rajasthan Result Bot\nChoose an option:",
            reply_markup=reply_markup
        )
    else:
        await update.callback_query.edit_message_text(
            "🏫 University of Rajasthan Result Bot\nChoose an option:",
            reply_markup=reply_markup
        )

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "main_cancel":
        await clear_session(update, context)
        return
    
    # Existing menu handling...

async def show_results_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Init loading message
        msg = await update.callback_query.edit_message_text(
            "⏳ Loading initial results... (0/100 loaded)"
        )
        
        # Start background tasks
        asyncio.create_task(process_results(context, msg.chat_id, msg.message_id))
        asyncio.create_task(update_progress(context, msg.chat_id, msg.message_id))
        
    except Exception as e:
        logger.error(f"Results error: {e}")

async def process_results(context, chat_id, message_id):
    """Process results with early return"""
    try:
        connector = aiohttp.TCPConnector(limit=50, ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            results = await fetch_results(session, DEFAULT_TIMEOUT)
            
            # Start session timer
            end_time = datetime.now() + timedelta(seconds=SESSION_DURATION)
            cache.active_sessions[chat_id] = {
                'message_id': message_id,
                'end_time': end_time
            }
            
            context.job_queue.run_once(
                update_session_timer,
                when=10,
                chat_id=chat_id,
                data={'message_id': message_id, 'end_time': end_time},
                name=f"session_{chat_id}"
            )
            
            # Show first page
            items = context.user_data.get('items', DEFAULT_ITEMS)
            keyboard = generate_keyboard(results, 0, items)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📋 {len(results)} results available:",
                reply_markup=keyboard
            )

    except Exception as e:
        logger.error(f"Processing error: {e}")

async def update_progress(context, chat_id, message_id):
    """Update loading progress every 2 seconds"""
    for _ in range(50):  # Max 100 seconds waiting
        if len(cache.results) >= MIN_RESULTS_TO_SHOW:
            break
            
        try:
            await context.bot.edit_message_text(
                f"⏳ Loading results... ({len(cache.results)}/100 loaded)",
                chat_id=chat_id,
                message_id=message_id
            )
            await asyncio.sleep(2)
        except:
            break

async def update_session_timer(context: ContextTypes.DEFAULT_TYPE):
    """Update session timer every 10 seconds"""
    job = context.job
    remaining = job.data['end_time'] - datetime.now()
    
    if remaining.total_seconds() <= 0:
        await clear_session_by_id(context, job.chat_id)
        return
    
    try:
        await context.bot.edit_message_text(
            f"🌐 Active Session | Time left: {remaining.seconds//60:02d}:{remaining.seconds%60:02d}\n"
            f"✅ Total results: {len(cache.results)}",
            chat_id=job.chat_id,
            message_id=job.data['message_id']
        )
        
        # Reschedule
        context.job_queue.run_once(
            update_session_timer,
            when=10,
            chat_id=job.chat_id,
            data=job.data,
            name=f"session_{job.chat_id}"
        )
    except Exception as e:
        logger.error(f"Timer error: {e}")

async def clear_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear user session completely"""
    chat_id = update.callback_query.message.chat_id
    await clear_session_by_id(context, chat_id)
    await update.callback_query.edit_message_text("✅ Session cleared successfully")

async def clear_session_by_id(context, chat_id):
    """Clear session by chat ID"""
    try:
        # Remove from cache
        if chat_id in cache.active_sessions:
            del cache.active_sessions[chat_id]
            
        # Cancel jobs
        for job in context.job_queue.get_jobs_by_name(f"session_{chat_id}"):
            job.schedule_removal()
            
        # Clear user data
        context.user_data.clear()
        
    except Exception as e:
        logger.error(f"Session clear error: {e}")
        
#async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await main_menu(update)

async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "main_results":
        await show_results_menu(update, context)
    elif query.data == "main_settings":
        await show_settings_menu(update, context)
    elif query.data == "main_help":
        await show_help_menu(update, context)
    elif query.data == "main_cancel":
        await clear_session(update, context)
    elif query.data == "main_menu":
        await main_menu(update)

#async def show_results_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['page'] = 0
    try:
        timeout = context.user_data.get('timeout', DEFAULT_TIMEOUT)
        items_per_page = context.user_data.get('items', DEFAULT_ITEMS)
        
        connector = aiohttp.TCPConnector(limit=50, ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            results = await fetch_results(session, timeout)
            if not results:
                await update.callback_query.edit_message_text("No results found. Please try again later.")
                return
            
            keyboard = generate_keyboard(results, 0, items_per_page)
            await update.callback_query.edit_message_text(
                "📝 Select a result to check:",
                reply_markup=keyboard
            )
    except Exception as e:
        logger.error(f"Results error: {e}")
        await update.callback_query.edit_message_text("🚨 Temporary server issue. Try again later.")

async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    timeout = context.user_data.get('timeout', DEFAULT_TIMEOUT)
    items = context.user_data.get('items', DEFAULT_ITEMS)
    
    keyboard = [
        [InlineKeyboardButton(f"⏱ Timeout: {timeout}s", callback_data="set_timeout")],
        [InlineKeyboardButton(f"📑 Items per page: {items}", callback_data="set_items")],
        [InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(
        "⚙️ Bot Settings - Adjust parameters:",
        reply_markup=reply_markup
    )

async def show_help_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📚 *University Result Bot Guide* 📚

*Main Features:*
1️⃣ *Results* - Check latest exam results
2️⃣ *Settings* - Configure bot behavior
3️⃣ *Help* - This guide

*How to Check Results:*
1. Select *Results* from main menu
2. Choose your exam from the list
3. Send your details in format:
   `ROLLNO DD-MM-YYYY`
   Example: `123456 01-01-2000`

*Settings Options:*
- *Timeout*: Adjust server wait time (8-300s)
- *Items per page*: Change results displayed (10-80)

*Tips:*
• Use official roll numbers
• Double-check date format
• Server responses may take 10-15 seconds
• Results cache refreshes every 15 minutes
"""
    await update.callback_query.edit_message_text(help_text, parse_mode='Markdown')
    await asyncio.sleep(15)
    await main_menu(update)

async def handle_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "set_timeout":
        await adjust_timeout(update, context)
    elif query.data == "set_items":
        await adjust_items(update, context)

async def adjust_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_timeout = context.user_data.get('timeout', DEFAULT_TIMEOUT)
    
    keyboard = [
        [
            InlineKeyboardButton("➖10s", callback_data="timeout_-10"),
            InlineKeyboardButton(f"{current_timeout}s", callback_data="none"),
            InlineKeyboardButton("➕10s", callback_data="timeout_+10")
        ],
        [InlineKeyboardButton("🔙 Back to Settings", callback_data="main_settings")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(
        f"⏱ Adjust Timeout ({MIN_TIMEOUT}-{MAX_TIMEOUT} seconds):",
        reply_markup=reply_markup
    )

async def adjust_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_items = context.user_data.get('items', DEFAULT_ITEMS)
    
    keyboard = [
        [
            InlineKeyboardButton("➖10", callback_data="items_-10"),
            InlineKeyboardButton(f"{current_items}", callback_data="none"),
            InlineKeyboardButton("➕10", callback_data="items_+10")
        ],
        [InlineKeyboardButton("🔙 Back to Settings", callback_data="main_settings")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(
        f"📑 Adjust Items Per Page ({MIN_ITEMS}-{MAX_ITEMS}):",
        reply_markup=reply_markup
    )

async def handle_adjustments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    
    try:
        setting_type = data[0]
        adjustment = int(data[1])
        
        current_value = context.user_data.get(setting_type, DEFAULT_TIMEOUT if setting_type == "timeout" else DEFAULT_ITEMS)
        new_value = current_value + adjustment
        
        # Apply constraints
        if setting_type == "timeout":
            new_value = max(MIN_TIMEOUT, min(MAX_TIMEOUT, new_value))
        else:
            new_value = max(MIN_ITEMS, min(MAX_ITEMS, new_value))
        
        context.user_data[setting_type] = new_value
        
        if setting_type == "timeout":
            await adjust_timeout(update, context)
        else:
            await adjust_items(update, context)
            
    except Exception as e:
        logger.error(f"Adjustment error: {e}")
        await query.edit_message_text("⚠️ Adjustment failed. Try again.")

async def handle_results_pagination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    try:
        current_page = context.user_data.get('page', 0)
        items_per_page = context.user_data.get('items', DEFAULT_ITEMS)
        
        if query.data == "page_next":
            new_page = current_page + 1
        elif query.data == "page_back":
            new_page = max(0, current_page - 1)
        else:
            return
            
        context.user_data['page'] = new_page
        keyboard = generate_keyboard(cache.results, new_page, items_per_page)
        await query.edit_message_text(
            "📝 Select a result to check:",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Pagination error: {e}")
        await query.edit_message_text("⚠️ Navigation failed. Try again.")

async def update_session_timer(context: ContextTypes.DEFAULT_TYPE):
    """Update the session timer every 10 seconds"""
    chat_id = context.job.chat_id
    message_id = context.job.data['message_id']
    remaining_time = context.job.data['remaining_time'] - 10
    
    if remaining_time <= 0:
        try:
            await context.bot.edit_message_text(
                text="⌛ Session expired. Please start over with /start",
                chat_id=chat_id,
                message_id=message_id
            )
        except Exception as e:
            logger.error(f"Timer expiration error: {e}")
        return

    # Update the message
    try:
        selected_result = context.bot_data.get(f'{chat_id}_selected_result', {})
        keyboard = [
            [InlineKeyboardButton(f"📌 Selected: {selected_result.get('name', '')}", callback_data="none")],
            [InlineKeyboardButton("◀️ Back to Results", callback_data="results_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await context.bot.edit_message_text(
            text=f"⏳ Session active ({remaining_time} seconds remaining):\n"
                 f"📝 Please send your:\n"
                 f"• Roll Number\n"
                 f"• Date of Birth (DD-MM-YYYY)\n\n"
                 f"Example: `123456 01-01-2000`",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
        # Reschedule the job if time remains
        if remaining_time > 0:
            context.job_queue.run_once(
                callback=update_session_timer,
                when=10,
                chat_id=chat_id,
                data={'message_id': message_id, 'remaining_time': remaining_time},
                name=f"session_timer_{chat_id}_{message_id}"
            )
    except Exception as e:
        logger.error(f"Timer update error: {e}")

async def handle_result_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    try:
        if query.data.startswith('result_'):
            selected_idx = int(query.data.split('_')[1])
            if selected_idx < len(cache.results):
                selected_result = cache.results[selected_idx]
                context.user_data['result_url'] = selected_result['url']
                context.bot_data[f'{query.message.chat_id}_selected_result'] = selected_result
                
                # Create buttons
                keyboard = [
                    [InlineKeyboardButton(f"📌 Selected: {selected_result['name']}", callback_data="none")],
                    [InlineKeyboardButton("◀️ Back to Results", callback_data="results_back")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                # Send message with timer
                msg = await query.edit_message_text(
                    f"⏳ Session active ({SESSION_TIMEOUT} seconds remaining):\n"
                    f"📝 Please send your:\n"
                    f"• Roll Number\n"
                    f"• Date of Birth (DD-MM-YYYY)\n\n"
                    f"Example: `123456 01-01-2000`",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                
                # Start timer
                context.job_queue.run_once(
                    callback=update_session_timer,
                    when=10,
                    chat_id=msg.chat_id,
                    data={'message_id': msg.message_id, 'remaining_time': SESSION_TIMEOUT},
                    name=f"session_timer_{msg.chat_id}_{msg.message_id}"
                )
            else:
                await query.edit_message_text("❌ Invalid selection")
    except Exception as e:
        logger.error(f"Selection error: {e}")
        await query.edit_message_text("⚠️ Please try again")

async def handle_results_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle back navigation from result selection"""
    query = update.callback_query
    await query.answer()
    
    try:
        # Cancel existing timer
        chat_id = query.message.chat_id
        message_id = query.message.message_id
        current_jobs = context.job_queue.get_jobs_by_name(f"session_timer_{chat_id}_{message_id}")
        for job in current_jobs:
            job.schedule_removal()
        
        # Return to results list
        items_per_page = context.user_data.get('items', DEFAULT_ITEMS)
        keyboard = generate_keyboard(cache.results, context.user_data.get('page', 0), items_per_page)
        await query.edit_message_text(
            "📝 Select a result to check:",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Back navigation error: {e}")
        await query.edit_message_text("⚠️ Failed to navigate back")

async def submit_result_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Cancel any existing timer
        chat_id = update.message.chat_id
        if f'{chat_id}_selected_result' in context.bot_data:
            del context.bot_data[f'{chat_id}_selected_result']
        
        current_jobs = context.job_queue.get_jobs_by_name(f"session_timer_{chat_id}_*")
        for job in current_jobs:
            job.schedule_removal()

        user_input = update.message.text.split()
        if len(user_input) != 2:
            await update.message.reply_text(
                "❌ Invalid format! Please send:\n"
                "ROLLNO DD-MM-YYYY\n"
                "Example: `123456 01-01-2000`",
                parse_mode='Markdown'
            )
            return

        rollno, dob_str = user_input
        
        # Convert DD-MM-YYYY to YYYY-MM-DD
        try:
            dob_date = datetime.strptime(dob_str, "%d-%m-%Y")
            formatted_dob = dob_date.strftime("%Y-%m-%d")
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid date format! Please use DD-MM-YYYY\n"
                "Example: `01-01-2000`",
                parse_mode='Markdown'
            )
            return

        result_url = context.user_data.get('result_url')
        if not result_url:
            await update.message.reply_text("❌ Session expired. Start over with /start")
            return

        timeout = context.user_data.get('timeout', DEFAULT_TIMEOUT)
        start_time = datetime.now()
        
        
        async with aiohttp.ClientSession() as session:
            try:
           
       	        async with session.post(
                    result_url,
                    data={'Studroll': rollno, 'Studdob': formatted_dob, 'OKbtn': 'Find'},
                    headers=await get_headers(),
                    timeout=timeout
                ) as response:
                    html_content = await response.text()

                    if response.status != 200:
                        raise Exception(f"HTTP Error {response.status}")

                    soup = BeautifulSoup(html_content, 'html.parser')
                
                    # कस्टम HTML टेम्पलेट
                    custom_html = f"""
                    <!DOCTYPE html>
                    <html>
                    <head>
                        <meta charset="UTF-8">
                        <title>🌸 RU Result - {rollno} 🌧️</title>
                        <style>
                            @import url('https://fonts.googleapis.com/css2?family=Pacifico&family=Roboto:wght@300;400;700&display=swap');

                            :root {{
                                --main-bg: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
                                --text-glow: 0 0 15px #4facfe;
                            }}

                            body {{
                                margin: 0;
                                min-height: 100vh;
                                background: var(--main-bg);
                                font-family: 'Roboto', sans-serif;
                                overflow-x: hidden;
                                position: relative;
                            }}

                            /* rain majik */
                            .rain {{
                                position: fixed;
                                height: 100%;
                                width: 100%;
                                pointer-events: none;
                                z-index: 1;
                            }}

                            .drop {{
                                position: absolute;
                                background: linear-gradient(transparent, #4facfe);
                                width: 2px;
                                height: 50px;
                                animation: fall 1s linear infinite;
                                opacity: 0.6;
                            }}

                            @keyframes fall {{
                                0% {{ transform: translateY(-100vh) rotate(15deg); }}
                                100% {{ transform: translateY(100vh) rotate(15deg); }}
                            }}

                            /* flower animation */
                            .flower {{
                                position: fixed;
                                font-size: 24px;
                                animation: float 8s linear infinite;
                                opacity: 0.5;
                                z-index: 0;
                            }}

                            @keyframes float {{
                                0% {{ transform: translateY(100vh) rotate(0deg); }}
                                100% {{ transform: translateY(-100vh) rotate(360deg); }}
                            }}

                            .header {{
                                padding: 2rem;
                                text-align: center;
                                background: rgba(0,0,0,0.2);
                                backdrop-filter: blur(10px);
                                border-radius: 20px;
                                margin: 20px;
                                box-shadow: 0 4px 30px rgba(0,0,0,0.1);
                                position: relative;
                                z-index: 2;
                            }}

                            .datetime {{
                                font-size: 1.8rem;
                                color: #fff;
                                text-shadow: var(--text-glow);
                                margin-bottom: 1rem;
                                font-family: 'Pacifico', cursive;
                                animation: pulse 2s ease-in-out infinite;
                            }}

                            @keyframes pulse {{
                                0%, 100% {{ transform: scale(1); }}
                                50% {{ transform: scale(1.05); }}
                            }}


                            /* Original Table स्टाइलिंग */
                            .original-table {{
                                background: rgba(255,255,255,0.75) !important;
                                backdrop-filter: blur(1px);
                                border-radius: 2px;
                                overflow: visible;
                                box-shadow: n8one;
                                margin: 0 auto;
                                width: 95% !important;
                            }}


                            .footer {{
                                text-align: center;
                                padding: 2rem;
                                margin: 20px;
                                position: relative;
                                z-index: 2;
                            }}

                            .magic-text {{
                                font-family: 'Pacifico', cursive;
                                font-size: 1.5em;
                                background: linear-gradient(45deg, #ff6b6b, #4ecdc4, #45b7d1);
                                -webkit-background-clip: text;
                                background-clip: text;
                                color: transparent;
                                animation: rainbow 5s ease infinite;
                                text-shadow: 0 0 10px rgba(78, 205, 196, 0.3);
                            }}

                            @keyframes rainbow {{
                                0% {{ background-position: 0% 50%; }}
                                50% {{ background-position: 100% 50%; }}
                                100% {{ background-position: 0% 50%; }}
                            }}
                        </style>
                        <script>
                            function createEffects() {{
                                // rain
                                const rainContainer = document.createElement('div');
                                rainContainer.className = 'rain';
                                for(let i=0; i<100; i++) {{
                                    const drop = document.createElement('div');
                                    drop.className = 'drop';
                                    drop.style.left = Math.random() * 100 + 'vw';
                                    drop.style.animationDelay = Math.random() * 2 + 's';
                                    rainContainer.appendChild(drop);
                                }}
                                document.body.prepend(rainContainer);

                            // flowers
                                const flowers = ['🌸', '🌺', '🌼', '🌷'];
                                for(let i=0; i<40; i++) {{
                                    const flower = document.createElement('div');
                                    flower.className = 'flower';
                                    flower.innerHTML = flowers[Math.floor(Math.random()*flowers.length)];
                                    flower.style.left = Math.random() * 100 + 'vw';
                                    flower.style.animationDelay = Math.random() * 5 + 's';
                                    document.body.appendChild(flower);
                                }}
                            }}

                            function updateDateTime() {{
                                const options = {{ 
                                    weekday: 'long', 
                                    year: 'numeric', 
                                    month: 'long', 
                                    day: 'numeric',
                                    hour: '2-digit',
                                    minute: '2-digit',
                                    second: '2-digit',
                                    hour12: true
                                }};
                                const now = new Date().toLocaleDateString('en-IN', options);
                                document.getElementById('datetime').innerHTML = 
                                    `✨ ${"{now.replace(/ at /, '<br>🎉 ')}"} ✨`;
                            }}
                        
                            window.onload = function() {{
                                createEffects();
                                setInterval(updateDateTime, 1000);
                                updateDateTime();
                            }};
                        </script>
                    </head>
                    <body>
                        <div class="header">
                            <div class="datetime" id="datetime"></div>
                            <h2 style="color: #fff; margin: 15px 0; font-size: 2.5em; text-shadow: 0 0 20px #4facfe;">
                                🎓 University of Rajasthan
                            </h2>
                            <h3 style="color: #ffd700; margin: 0; font-size: 1.8em;">
                                🏷️ Roll Number: {rollno}
                            </h3>
                        </div>

                    """

                    # Original table को बिना बदलाव के लाना
                    result_table = soup.find('table', {'class': 'print'})
                    if result_table:
                        # Original table की सभी प्रॉपर्टीज रखें
                        custom_html += str(result_table).replace('<table', '<table class="original-table"')
                    else:
                        error_message = soup.find('td').get_text(strip=True)
                        custom_html += f"""
                        <div style="color: white; text-align: center; padding: 50px;">
                            {error_message}
                        </div>
                        """


                    

                    # Main result processing part
                    result_table = soup.find('table', {'class': 'print'})

                    if result_table:
                        # पूरा टेबल सेक्शन लें (मुख्य टेबल + नोट्स)
                        table_section = result_table.find_parent('table')
    
                        # स्टाइलिंग जोड़ें
                        custom_html += """
                        <style>
                            .disclaimer-notes {
                                margin-top: 20px;
                                padding: 15px;
                                background: rgba(255,255,255,0.9);
                                border-radius: 10px;
                                font-size: 0.9em;
                                color: #444;
                                border-left: 4px solid #e94560;
                            }
                            .disclaimer-notes font {
                                color: #d35400 !important;
                            }
                        </style>
                        """
    
                        # मुख्य टेबल जोड़ें
                        custom_html += str(table_section).replace('<table', '<table class="original-table"')
    
                        # नोट्स को अलग से जोड़ें (अगर मौजूद हों)
                        notes = result_table.find_next_siblings('tr')
                        if notes:
                            custom_html += '<div class="disclaimer-notes">'
                            for note in notes:
                                custom_html += str(note)
                            custom_html += '</div>'

                    else:
                        error_msg = soup.find('td').get_text(strip=True)
                        custom_html += f"""
                        <div style="color: white; text-align: center; padding: 50px; font-size: 1.5em;">
                            ❌ {error_msg}
                        </div>
                        """


                    # फुटर जोड़ें
                    custom_html += f"""
                        <div class="footer">
                            <div class="magic-text">
                                GENERATED BY @UNIRAJ_JAIPUR<br>
                                ⚡ Powered by Rajasthan University ⚡<br>
                                🕒 {datetime.now().strftime("%d-%m-%Y %H:%M:%S")}
                            </div>
                        </div>
                    </body>
                    </html>
                    """

                    # HTML फ़ाइल सेव करें और भेजें
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
                        f.write(custom_html)
                        temp_path = f.name

                    with open(temp_path, 'rb') as f:
                        await update.message.reply_document(
                            document=f,
                            filename=f"result_{rollno}.html",
                            caption=f"✨ Result for Roll No: {rollno} ✨",
                            write_timeout=20
                        )
                
                    os.unlink(temp_path)
                    logger.info(f"Result processed in {datetime.now() - start_time}")

            except Exception as e:
                logger.error(f"Error: {str(e)}")
                await update.message.reply_text("⚠️ Error processing request. Please try again later.")

    except Exception as e:
            logger.error(f"Final error: {e}")
            await update.message.reply_text("🚨 An error occurred. Start over with /start")

def main() -> None:
    start_dummy_server()

    TOKEN = os.getenv("BOT_TOKEN")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")

    application = Application.builder() \
        .token(TOKEN) \
        .job_queue(JobQueue()) \
        .build()

    
    # Command handlers
    application.add_handler(CommandHandler("start", start))

    # Main menu handlers
    application.add_handler(CallbackQueryHandler(handle_main_menu, pattern="^main_"))
    application.add_handler(CallbackQueryHandler(handle_settings, pattern="^set_"))
    application.add_handler(CallbackQueryHandler(handle_adjustments, pattern="^(timeout|items)_"))
    
    # Results navigation
    application.add_handler(CallbackQueryHandler(handle_results_pagination, pattern="^page_"))
    application.add_handler(CallbackQueryHandler(handle_result_selection, pattern="^result_"))
    application.add_handler(CallbackQueryHandler(handle_results_back, pattern="^results_back$"))
    
    # Message handler for result submission
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, submit_result_request))

    # Run the bot
    application.run_polling()

if __name__ == "__main__":
    main()
 
