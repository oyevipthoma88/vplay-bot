# VPlay — Telegram Video-Chat Player Bot

[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/oyevipthoma88/vplay-bot)

Ek Telegram bot jo group ke **video/voice chat** mein seedha video/audio play karta hai. Koi YouTube search, cookies, ya login-wall nahi — bas jo media aap doge (reply karke ya direct link se), wahi turant play hoga, aur poore playback controls (pause/resume/skip/seek/queue) ke saath.

> **Deploy button note:** GitHub repository privacy ke kaaran repo abhi private hai. Heroku ka one-click deploy flow private GitHub template ko access nahi kar sakta. Button ko use karne se pehle repo ko public karna hoga, ya repo ko apne account ke kisi public mirror/template mein rakhna hoga. Private repo ko bina public kiye deploy karne ke liye Heroku Dashboard se app bana kar isi repository ko deploy source ke roop mein configure karein.

## v1.5 — kya naya hai

- **/vplay naam** ab channel ke local index se search karta hai — turant result, spelling galti chalegi (`mirjapur` → Mirzapur, `hanumanansh` → Hanuman Ansh), file name *aur* caption dono me dhoondta hai.
- **Latest upload sabse upar.** Ek hi result ho to seedha play; zyada results ho to rangeen buttons (🟢 newest, 🔵 baaki, 🔴 cancel) + pages. Jo button dabaoge wahi play hoga.
- **Nayi upload turant milti hai** — channel me post hote hi index me add (bot + assistant dono sunte hain). Delete/edit bhi update hota hai.
- **Quality drop fix:** pehle ek hi movie Telegram se 2 baar (audio + video) download hoti thi; ab ek shared cache se dono padhte hain. Fast crypto (`tgcrypto-pyrofork`) bhi add kiya.
- **Fast start:** /vplay par file ka pehla hissa pehle se download hone lagta hai.
- `/vindex` — index status · `/vindex rebuild` — owner dobara scan karwa sakta hai.

## Features

- `/vplay` — kisi video/audio file par reply karke, ya `/vplay <direct link>` bhejo — turant play ya queue
- Pause, resume, skip, seek (`/vseek 90` ya `/vseek 1:30`), stop
- Multi-track queue — ek ke baad ek play hote rehte hain
- Mute/unmute
- Group admins-only control (chahein to disable kar sakte hain)
- Koi cookies/YouTube integration nahi — sirf jo media aap doge

## Commands

| Command | Kaam |
|---|---|
| `/vplay` (reply) ya `/vplay <link>` | Play ya queue |
| `/vpause` | Pause |
| `/vresume` | Resume |
| `/vskip` | Skip → queue ka agla track |
| `/vseek <sec ya mm:ss>` | Seek |
| `/vstop` | Stop + video chat chhodna |
| `/vqueue` | Queue dekhna |
| `/vnowplaying` | Abhi kya chal raha hai |
| `/vmute` / `/vunmute` | Assistant ko mute/unmute |
| `/ping` | Bot alive check |

## Ye kaam kaise karta hai

Telegram ke normal **bot accounts group video/voice chat mein khud join nahi kar sakte** — isliye is tarah ke bots do accounts use karte hain:

1. **Bot account** (`BOT_TOKEN`) — commands sunta hai (`/vplay`, `/vskip`, ...)
2. **Assistant account** (`SESSION_STRING`) — ek normal Telegram user account, jo actually group ke video chat mein join hokar stream karta hai ([PyTgCalls](https://github.com/pytgcalls/pytgcalls) library ke through)

Assistant account jis bhi group mein video chalana ho, wahan **member hona zaroori hai, aur admin hona strongly recommended** (Manage Video Chats permission ke saath).

Media source do tarah ka ho sakta hai:

- **Direct link** — ffmpeg seedha usi link se stream karta hai, isliye ye practically turant start ho jata hai.
- **Reply ki hui Telegram file** — bot pehle use download karta hai, phir play karta hai.

## Setup — credentials lena

1. **API_ID + API_HASH** — [my.telegram.org](https://my.telegram.org) par login karke “API Development Tools” mein banayein.
2. **BOT_TOKEN** — Telegram par [@BotFather](https://t.me/BotFather) ko `/newbot` bhejein.
3. **OWNER_ID** — apna numeric Telegram user ID; [@userinfobot](https://t.me/userinfobot) se pata chal jayega.
4. **SESSION_STRING** — apne computer par (Heroku par nahi):
   ```bash
   git clone https://github.com/oyevipthoma88/vplay-bot.git
   cd vplay-bot
   pip install kurigram tgcrypto
   python3 generate_session.py
   ```
   Ye same phone number/OTP maangega jis account ko assistant banana hai. Session string ko safe rakhein — ye normal login jaisa sensitive hai.

## Deploy to Heroku

`Procfile`, `app.json`, `Aptfile`, aur Python dependencies Heroku worker dyno ke liye configured hain.

### One-click deploy

1. Repo ko public banayein (Heroku deploy button private GitHub repo ko read nahi kar sakta).
2. README ke upar **Deploy to Heroku** button dabayein.
3. Heroku form mein `API_ID`, `API_HASH`, `BOT_TOKEN`, `SESSION_STRING`, aur `OWNER_ID` fill karke **Deploy App** dabayein.
4. App create hone ke baad worker dyno enabled hona chahiye. Logs se confirm karein:
   ```bash
   heroku logs --tail -a <app-name>
   ```

### Private repo deploy

Repo private rakhna ho to Heroku Dashboard mein manually app create karke GitHub repository connect karein, phir worker dyno enable karke wahi config vars set karein. Heroku ki GitHub integration ko GitHub authorization dena padega.

## Local run

```bash
cp .env.sample .env
# .env mein apni values bharein
pip install -r requirements.txt
python3 -m bot
```

Local run ke liye `ffmpeg` installed hona chahiye (`sudo apt install ffmpeg` / `brew install ffmpeg`).

## Troubleshooting

- **Video/voice chat se connect nahi ho paya** — group ka video/voice chat pehle start karein, aur assistant account ko group mein admin banayein (Manage Video Chats permission).
- **ffmpeg not found** — Heroku par ye `Aptfile` se automatic install hota hai; local run mein khud install karna hoga.
- **PyTgCalls / Kurigram compatibility issue** — agar join hi na ho, `requirements.txt` mein `py-tgcalls` ki jagah `basiccalls` try karein:
  ```text
  basiccalls>=2.2.9,<3.0.0
  ```
  Import path wahi (`from pytgcalls import ...`) rehta hai.
- **Deploy button kaam nahi kar raha** — repo public hai ya nahi check karein, aur `app.json` ka `repository` URL verify karein.

## Limitations

Jaan-boojh kar simple rakha gaya hai: koi YouTube/Spotify/Saavn search, cookies, ya auth integration nahi — sirf jo media aap directly doge (link ya reply).

## License

MIT — [LICENSE](./LICENSE) dekhein.
