require('dotenv').config();
const fs = require('fs');
const path = require('path');
const express = require('express');
const { Telegraf, Markup } = require('telegraf');
const axios = require('axios');
const sharp = require('sharp');
const OpenAI = require('openai');

const TELEGRAM_TOKEN = process.env.TELEGRAM_TOKEN;
const OPENAI_API_KEY = process.env.OPENAI_API_KEY;
const OPENAI_MODEL = process.env.OPENAI_MODEL || 'gpt-4o'; // غيّره إن لزم
const AFFILIATE_TAG = process.env.AFFILIATE_TAG || 'chop07c-20';
const BASE_URL = process.env.BASE_URL; // مثال: https://your-app.up.railway.app
const PORT = process.env.PORT || 3000;

if (!TELEGRAM_TOKEN || !OPENAI_API_KEY || !BASE_URL) {
  console.error('Required env missing. Set TELEGRAM_TOKEN, OPENAI_API_KEY, and BASE_URL.');
  process.exit(1);
}

const bot = new Telegraf(TELEGRAM_TOKEN);
const openai = new OpenAI({ apiKey: OPENAI_API_KEY });
const app = express();
app.use(express.json());

// ----------------- persistence for clicks -----------------
const CLICKS_FILE = path.join(__dirname, 'clicks.json');
let clicks = {};
try {
  if (fs.existsSync(CLICKS_FILE)) {
    clicks = JSON.parse(fs.readFileSync(CLICKS_FILE, 'utf8') || '{}');
  } else {
    fs.writeFileSync(CLICKS_FILE, JSON.stringify({}), 'utf8');
  }
} catch (e) {
  console.error('Failed loading clicks file:', e);
  clicks = {};
}
function saveClicks() {
  try {
    fs.writeFileSync(CLICKS_FILE, JSON.stringify(clicks, null, 2), 'utf8');
  } catch (e) { console.error('Failed to save clicks:', e); }
}

// ----------------- helper: language & domains -----------------
function detectLangCode(code) {
  if (!code) return 'en';
  code = code.toLowerCase();
  if (code.startsWith('ar')) return 'ar';
  if (code.startsWith('fr')) return 'fr';
  return 'en';
}
function domainForLang(lang) {
  // map languages to sensible Amazon domains
  if (lang === 'ar') return 'www.amazon.sa'; // Saudi / Arabic market (good default)
  if (lang === 'fr') return 'www.amazon.fr';
  return 'www.amazon.com';
}

// ----------------- UI text -----------------
const TEXT = {
  welcome: {
    ar: `👋 أهلاً بك!\n\nأرسل صورة لمنتج وسأحاول التعرف عليه ثم أعطيك أزرار شراء من أمازون (مع رابط العمولة).`,
    en: `👋 Hello!\n\nSend a product image and I'll identify it and give you purchase buttons on Amazon (affiliate link included).`,
    fr: `👋 Bonjour !\n\nEnvoyez une image d'un produit et je l'identifierai puis je fournirai des boutons d'achat Amazon (lien d'affiliation inclus).`
  },
  howto: {
    ar: `📌 نصائح لنتيجة أفضل:\n• صوّر الملصق أو المنتج بوضوح\n• اجعل الإضاءة جيدة\n• تجنّب الزوايا البعيدة`,
    en: `📌 Tips for better results:\n• Capture the label or product clearly\n• Use good lighting\n• Avoid far/distorted angles`,
    fr: `📌 Conseils pour de meilleurs résultats :\n• Photographiez clairement l'étiquette ou le produit\n• Bonne luminosité\n• Évitez les angles lointains`
  },
  processing: { ar: '⏳ جارٍ التحليل...', en: '⏳ Analyzing...', fr: '⏳ Analyse en cours...' },
  notfound: { ar: '❌ لم أتمكن من التعرف على المنتج.', en: '❌ Could not identify the product.', fr: '❌ Produit non reconnu.' },
  buy: { ar: '🛒 شراء (محلي)', en: '🛒 Buy (local)', fr: '🛒 Acheter (local)' },
  buy_us: { ar: '🌐 شراء (Amazon.com)', en: '🌐 Buy (Amazon.com)', fr: '🌐 Acheter (Amazon.com)' },
  more: { ar: '🖼️ صورة جديدة', en: '🖼️ New image', fr: '🖼️ Nouvelle image' },
  clicks: { ar: 'نقراتك:', en: 'Your clicks:', fr: 'Vos clics:' }
};

// ----------------- amazon link generator -----------------
function amazonSearchLink(product, domain = 'www.amazon.com') {
  const q = encodeURIComponent(product);
  return `https://${domain}/s?k=${q}&tag=${AFFILIATE_TAG}`;
}

// ----------------- download & compress image -----------------
async function downloadTelegramFile(fileId) {
  const file = await bot.telegram.getFile(fileId);
  const url = `https://api.telegram.org/file/bot${TELEGRAM_TOKEN}/${file.file_path}`;
  const res = await axios.get(url, { responseType: 'arraybuffer' });
  return Buffer.from(res.data);
}
async function compressImage(buf) {
  return sharp(buf).rotate().resize({ width: 1024, withoutEnlargement: true }).jpeg({ quality: 72 }).toBuffer();
}

// ----------------- call OpenAI Vision and request JSON -----------------
async function identifyProductMultilang(imageBuffer) {
  const b64 = imageBuffer.toString('base64');

  // Instruction: return strict JSON only
  const instruction = `
You will receive an image. Identify the product and return a STRICT JSON object ONLY (no extra text) with these keys:
{
  "name_en": "short product name in English",
  "name_ar": "short product name in Arabic",
  "name_fr": "short product name in French"
}
Return empty strings if you cannot produce a translation. Keep names concise (one-line).
  `.trim();

  const resp = await openai.responses.create({
    model: OPENAI_MODEL,
    input: [
      {
        role: 'user',
        content: [
          { type: 'input_text', text: instruction },
          { type: 'input_image', image: `data:image/jpeg;base64,${b64}` }
        ]
      }
    ],
    max_output_tokens: 200
  });

  // robust parsing: try output_text first, then try to extract JSON-like from output
  let text = '';
  try {
    if (resp.output_text) text = resp.output_text;
    else if (Array.isArray(resp.output) && resp.output.length) {
      // attempt to join message content
      text = resp.output.map(o => {
        if (o.content) return o.content.map(c => c.text || '').join(' ');
        return (o.text || '');
      }).join(' ');
    } else if (resp.output?.[0]?.content?.[0]?.text) {
      text = resp.output[0].content[0].text;
    }
  } catch (e) { text = ''; }

  // try to extract JSON substring
  let jsonStr = null;
  try {
    const start = text.indexOf('{');
    const end = text.lastIndexOf('}');
    if (start !== -1 && end !== -1 && end > start) {
      jsonStr = text.slice(start, end + 1);
    } else {
      jsonStr = text;
    }
    const parsed = JSON.parse(jsonStr);
    return {
      en: (parsed.name_en || parsed.name || '').trim(),
      ar: (parsed.name_ar || parsed.name_in_arabic || '').trim(),
      fr: (parsed.name_fr || parsed.name_fr || '').trim()
    };
  } catch (e) {
    // fallback: return the plain text as English, empty others
    return { en: (text || '').trim(), ar: '', fr: '' };
  }
}

// ----------------- bot handlers -----------------
bot.start(async ctx => {
  const lang = detectLangCode(ctx.from.language_code);
  await ctx.replyWithMarkdown(TEXT.welcome[lang] + '\n\n' + TEXT.howto[lang]);
});

bot.help(async ctx => {
  const lang = detectLangCode(ctx.from.language_code);
  await ctx.reply(TEXT.howto[lang]);
});

bot.on('photo', async ctx => {
  const lang = detectLangCode(ctx.from.language_code);
  await ctx.reply(TEXT.processing[lang]);

  try {
    const photos = ctx.message.photo;
    const largest = photos[photos.length - 1];
    const raw = await downloadTelegramFile(largest.file_id);
    const compressed = await compressImage(raw);

    const ids = await ctx.reply('🔎 Sending image to vision model...');
    const product = await identifyProductMultilang(compressed);

    if (!product || (!product.en && !product.ar && !product.fr)) {
      return ctx.reply(TEXT.notfound[lang]);
    }

    // choose domain based on user language
    const localDomain = domainForLang(lang);
    const usDomain = 'www.amazon.com';
    const otherDomain = lang === 'fr' ? 'www.amazon.com' : (lang === 'ar' ? 'www.amazon.fr' : 'www.amazon.fr');

    // prepare buttons via our /go redirect to record clicks
    // create encoded product query param
    const productEncoded = encodeURIComponent(product.en || product.ar || product.fr || 'product');

    const localUrl = `${BASE_URL}/go?product=${productEncoded}&domain=${encodeURIComponent(localDomain)}&uid=${ctx.from.id}`;
    const usUrl = `${BASE_URL}/go?product=${productEncoded}&domain=${encodeURIComponent(usDomain)}&uid=${ctx.from.id}`;
    const otherUrl = `${BASE_URL}/go?product=${productEncoded}&domain=${encodeURIComponent(otherDomain)}&uid=${ctx.from.id}`;

    // show the identified names and buttons
    const nameDisplay = `📦 *Product (EN):* ${product.en || '-'}\n📝 *العربي:* ${product.ar || '-'}\n🇫🇷 *Français:* ${product.fr || '-'}`;

    await ctx.replyWithMarkdown(nameDisplay,
      Markup.inlineKeyboard([
        [ Markup.button.url(TEXT.buy[lang], localUrl) ],
        [ Markup.button.url(TEXT.buy_us[lang], usUrl), Markup.button.url('🌍 Other', otherUrl) ],
        [ Markup.button.callback(TEXT.more[lang], 'NEW_IMG') ]
      ])
    );

    // show clicks count for this user if exists
    const userClicks = clicks[ctx.from.id]?.total || 0;
    await ctx.reply(`${TEXT.clicks[lang]} ${userClicks}`);
  } catch (e) {
    console.error('photo handler err', e);
    await ctx.reply('⚠️ Error while processing. Try a different photo.');
  }
});

// reply to the "New image" callback
bot.action('NEW_IMG', async ctx => {
  const lang = detectLangCode(ctx.from.language_code);
  await ctx.reply(TEXT.welcome[lang]);
  try { await ctx.answerCbQuery(); } catch(e) {}
});

// ----------------- redirect endpoint to count clicks then forward -----------------
app.get('/go', (req, res) => {
  try {
    const { product = '', domain = 'www.amazon.com', uid = 'anon' } = req.query;
    const p = decodeURIComponent(product);
    const d = decodeURIComponent(domain);
    const userId = String(uid);

    // increment clicks: structure { userId: { total: N, byProduct: { product: N } } }
    if (!clicks[userId]) clicks[userId] = { total: 0, byProduct: {} };
    clicks[userId].total += 1;
    const key = p.toLowerCase();
    clicks[userId].byProduct[key] = (clicks[userId].byProduct[key] || 0) + 1;
    saveClicks();

    const redirectUrl = amazonSearchLink(p, d);
    return res.redirect(302, redirectUrl);
  } catch (e) {
    console.error('/go error', e);
    return res.status(500).send('Server error');
  }
});

// endpoint to inspect clicks (admin convenience) — optional, not protected (you can remove or protect it)
app.get('/clicks', (req, res) => {
  res.json(clicks);
});

// webhook route for Telegram
app.post('/webhook', (req, res) => {
  bot.handleUpdate(req.body, res).catch(err => {
    console.error('handleUpdate error', err);
  });
  res.status(200).send('OK');
});

// root
app.get('/', (req, res) => res.send('OK'));

// start server and set telegram webhook
app.listen(PORT, async () => {
  console.log('Server listening on', PORT);
  try {
    const webhookUrl = `${BASE_URL}/webhook`;
    await bot.telegram.setWebhook(webhookUrl);
    console.log('Webhook set to', webhookUrl);
  } catch (e) {
    console.error('Failed to set webhook:', e);
  }
});
