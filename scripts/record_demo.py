"""Запись демонстрационного видео: Playwright проходит сценарий в интерфейсе, поверх страницы рисуются курсор,
«тапы» на нажатиях и подписи к шагам, кадры снимаются через протокол Chrome (screencast) и собираются в MP4 (H.264).

    python scripts/record_demo.py --url http://localhost:8601 --out demo/navigator_demo.mp4

Требования: pip install playwright imageio-ffmpeg; установленный Google Chrome (или `playwright install chromium` и --channel "").
Интерфейс должен быть запущен на базе документов организаторов (make ui-org).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.async_api import Locator, Page, async_playwright, expect

VW, VH = 1440, 810          # окно браузера в CSS-пикселях: интерфейс крупнее и плотнее
DSF = 4 / 3                 # масштаб пикселей: кадр 1920×1080 без потери чёткости
W, H = 1920, 1080           # размер видео
CHAT_PLACEHOLDER = "Например: как оформить служебную командировку?"
ANSWERS = '[class*="st-key-ans-"]'   # контейнер каждого отрисованного ответа
CITES = '[class*="st-key-cite-"]'    # строки источников под шагом

OVERLAY_JS = r"""
(() => {
  if (window.__demo && document.getElementById('demo-cursor')) return;
  const css = `
  #demo-cursor{position:fixed;left:0;top:0;width:26px;height:26px;z-index:2147483647;pointer-events:none;
     transform:translate(720px,405px);transition-property:transform;transition-timing-function:cubic-bezier(.22,.61,.36,1);
     filter:drop-shadow(0 3px 4px rgba(0,0,0,.35))}
  .demo-tap{position:fixed;z-index:2147483646;pointer-events:none;border-radius:50%;width:22px;height:22px;margin:-11px 0 0 -11px;
     background:rgba(37,99,235,.30);border:3px solid rgba(37,99,235,.95);animation:demo-tap .7s ease-out forwards}
  .demo-tap.r2{animation-delay:.12s;opacity:0}
  @keyframes demo-tap{0%{transform:scale(.5);opacity:1}100%{transform:scale(3.6);opacity:0}}
  #demo-caption{position:fixed;left:50%;bottom:96px;transform:translateX(-50%) translateY(16px);z-index:2147483645;max-width:74%;
     padding:12px 20px;border-radius:13px;background:rgba(15,23,42,.90);color:#fff;text-align:center;
     font:600 19px/1.38 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;opacity:0;transition:opacity .35s,transform .35s;
     pointer-events:none;box-shadow:0 10px 30px rgba(0,0,0,.28)}
  #demo-caption.on{opacity:1;transform:translateX(-50%) translateY(0)}
  #demo-caption .st{display:inline-block;margin-right:12px;padding:2px 12px;border-radius:999px;background:#2563eb;font-size:15px;vertical-align:2px}
  #demo-key{position:fixed;z-index:2147483646;pointer-events:none;padding:7px 14px;border-radius:9px;background:#fff;color:#0f172a;
     border:1px solid #cbd5e1;box-shadow:0 4px 0 #94a3b8;font:700 15px -apple-system,sans-serif;opacity:0;transition:opacity .2s}
  #demo-card{position:fixed;inset:0;z-index:2147483647;display:flex;flex-direction:column;align-items:center;justify-content:center;
     background:linear-gradient(135deg,#0b2350 0%,#1e40af 100%);color:#fff;text-align:center;padding:60px;
     font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;opacity:0;transition:opacity .7s;pointer-events:none}
  #demo-card.on{opacity:1}
  #demo-card h1{font-size:48px;margin:0 0 18px;letter-spacing:-.5px}
  #demo-card p{font-size:24px;margin:6px 0;opacity:.94}
  #demo-card .small{font-size:18px;opacity:.75;margin-top:20px}
  .demo-hl{outline:4px solid rgba(250,204,21,.95)!important;outline-offset:4px;border-radius:8px}
  `;
  const st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);
  const cur = document.createElement('div'); cur.id = 'demo-cursor';
  cur.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26"><path d="M4 2.5l15.5 8.6-6.9 1.8-3.1 6.7z" fill="#fff" stroke="#0f172a" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  const cap = document.createElement('div'); cap.id = 'demo-caption';
  const key = document.createElement('div'); key.id = 'demo-key';
  const card = document.createElement('div'); card.id = 'demo-card';
  document.body.append(cur, cap, key, card);
  const scroller = () => {
    const m = document.querySelector('.stMain');
    if (m && m.scrollHeight > m.clientHeight + 5) return m;
    return document.scrollingElement;
  };
  const ease = k => k < .5 ? 4*k*k*k : 1 - Math.pow(-2*k + 2, 3) / 2;
  window.__demo = {
    move(x, y, d) { cur.style.transitionDuration = d + 's'; cur.style.transform = `translate(${x - 5}px, ${y - 3}px)`; },
    tap(x, y) {
      for (const cls of ['demo-tap', 'demo-tap r2']) {
        const t = document.createElement('div'); t.className = cls; t.style.left = x + 'px'; t.style.top = y + 'px';
        document.body.appendChild(t); setTimeout(() => t.remove(), 1000);
      }
    },
    caption(html) { if (!html) { cap.classList.remove('on'); return; } cap.innerHTML = html; cap.classList.add('on'); },
    key(label, x, y) { key.textContent = label; key.style.left = (x + 18) + 'px'; key.style.top = (y - 46) + 'px'; key.style.opacity = 1;
                       setTimeout(() => key.style.opacity = 0, 1000); },
    card(html) { if (!html) { card.classList.remove('on'); return; } card.innerHTML = html; card.classList.add('on'); },
    scrollTo(target, ms) {
      const sc = scroller(); const start = sc.scrollTop; const t0 = performance.now();
      const max = sc.scrollHeight - sc.clientHeight; target = Math.max(0, Math.min(max, target));
      return new Promise(res => { const step = t => { const k = Math.min(1, (t - t0) / ms); sc.scrollTop = start + (target - start) * ease(k);
        if (k < 1) requestAnimationFrame(step); else res(sc.scrollTop); }; requestAnimationFrame(step); });
    },
    scrollToEl(el, offset, ms) {
      const sc = scroller(); const r = el.getBoundingClientRect();
      const top = sc === document.scrollingElement ? 0 : sc.getBoundingClientRect().top;
      return this.scrollTo(sc.scrollTop + r.top - top - offset, ms);
    },
    scrollBy(dy, ms) { const sc = scroller(); return this.scrollTo(sc.scrollTop + dy, ms); },
  };
})();
"""


class Recorder:
    """Снимает кадры через CDP screencast и собирает видео с исходными интервалами между кадрами."""

    def __init__(self, page: Page, frames_dir: Path):
        self.page = page
        self.dir = frames_dir
        self.frames: list[tuple[float, Path]] = []
        self.cdp = None
        self.t_end = 0.0

    async def start(self) -> None:
        self.cdp = await self.page.context.new_cdp_session(self.page)

        async def on_frame(params: dict) -> None:
            ts = float(params["metadata"]["timestamp"])
            path = self.dir / f"{len(self.frames):06d}.jpg"
            path.write_bytes(base64.b64decode(params["data"]))
            self.frames.append((ts, path))
            try:
                await self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            except Exception:  # noqa: BLE001
                pass

        self.cdp.on("Page.screencastFrame", lambda p: asyncio.ensure_future(on_frame(p)))
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": W, "maxHeight": H, "everyNthFrame": 1})

    async def stop(self) -> None:
        self.t_end = time.time()
        await self.cdp.send("Page.stopScreencast")
        await asyncio.sleep(0.3)

    def encode(self, out: Path, ffmpeg: str, fps: int = 30) -> None:
        if not self.frames:
            raise RuntimeError("кадры не получены")
        frames = sorted(self.frames)
        lines = ["ffconcat version 1.0"]
        for i, (ts, path) in enumerate(frames):
            nxt = frames[i + 1][0] if i + 1 < len(frames) else max(ts + 0.5, self.t_end)
            lines.append(f"file '{path.name}'")
            lines.append(f"duration {max(1 / 60, nxt - ts):.4f}")
        lines.append(f"file '{frames[-1][1].name}'")
        (self.dir / "list.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(self.dir / "list.txt"),
               "-vf", f"fps={fps},scale={W}:{H}:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "21",
               "-movflags", "+faststart", str(out)]
        subprocess.run(cmd, check=True)


class Director:
    """Действия с видимым курсором, «тапами» и подписями."""

    def __init__(self, page: Page):
        self.page = page
        self.cx, self.cy = VW / 2, VH / 2
        self.failures: list[str] = []
        self.last: tuple[str | None, str | None] = (None, None)

    async def ensure(self) -> None:
        await self.page.evaluate(OVERLAY_JS)

    async def caption(self, text: str | None, step: str | None = None, remember: bool = True) -> None:
        await self.ensure()
        if remember:
            self.last = (text, step)
        html = None if text is None else (f'<span class="st">{step}</span>' if step else "") + text
        await self.page.evaluate("h => window.__demo.caption(h)", html)

    async def card(self, html: str | None) -> None:
        await self.ensure()
        await self.page.evaluate("h => window.__demo.card(h)", html)

    async def visible(self, loc: Locator, offset: float = 0.32) -> None:
        box = await loc.bounding_box()
        if box is None:
            await loc.scroll_into_view_if_needed()
            return
        if box["y"] < 60 or box["y"] + min(box["height"], 240) > VH - 140:
            await loc.evaluate("(el, a) => window.__demo.scrollToEl(el, a[0], a[1])", [VH * offset, 900])
            await asyncio.sleep(0.25)

    async def point(self, loc: Locator, fx: float = 0.5, fy: float = 0.5) -> tuple[float, float]:
        await self.ensure()
        await self.visible(loc)
        box = await loc.bounding_box()
        x, y = box["x"] + box["width"] * fx, box["y"] + box["height"] * fy
        dist = ((x - self.cx) ** 2 + (y - self.cy) ** 2) ** 0.5
        dur = min(0.95, max(0.35, dist / 1150))
        await self.page.evaluate("a => window.__demo.move(a[0], a[1], a[2])", [x, y, dur])
        await asyncio.sleep(dur + 0.1)
        self.cx, self.cy = x, y
        return x, y

    async def click(self, loc: Locator, fx: float = 0.5, fy: float = 0.5, pause: float = 0.45) -> None:
        x, y = await self.point(loc, fx, fy)
        await self.page.evaluate("a => window.__demo.tap(a[0], a[1])", [x, y])
        await asyncio.sleep(0.15)
        await loc.click(position={"x": (await loc.bounding_box())["width"] * fx, "y": (await loc.bounding_box())["height"] * fy})
        await asyncio.sleep(pause)

    async def type(self, loc: Locator, text: str) -> None:
        await self.click(loc, fx=0.3)
        await loc.press_sequentially(text, delay=38)
        await asyncio.sleep(0.35)

    async def enter(self, loc: Locator) -> None:
        await self.page.evaluate("a => window.__demo.key('Enter ↵', a[0], a[1])", [self.cx, self.cy])
        await self.page.evaluate("a => window.__demo.tap(a[0], a[1])", [self.cx, self.cy])
        await loc.press("Enter")

    async def scroll_by(self, dy: float, ms: int = 1100, pause: float = 1.2) -> None:
        await self.page.evaluate("a => window.__demo.scrollBy(a[0], a[1])", [dy, ms])
        await asyncio.sleep(pause)

    async def scroll_to(self, loc: Locator, offset: float = 90, ms: int = 1000, pause: float = 0.8) -> None:
        await loc.evaluate("(el, a) => window.__demo.scrollToEl(el, a[0], a[1])", [offset, ms])
        await asyncio.sleep(pause)

    async def highlight(self, loc: Locator, on: bool = True) -> None:
        await loc.evaluate("(el, on) => el.classList.toggle('demo-hl', on)", on)

    async def step(self, name: str, coro) -> None:
        try:
            await coro
        except Exception as e:  # noqa: BLE001  шаг не удался: запись продолжается, причина печатается
            self.failures.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
            print(f"!! шаг «{name}» не удался: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)


async def ask(d: Director, question: str, n_answers: int, wait_caption: str = "Система ищет пункты документов и проверяет каждый шаг…") -> Locator:
    """Набирает вопрос, ждёт ответ номер n_answers и возвращает его контейнер. Интерфейс сам прокручивает ленту к началу нового ответа."""
    page = d.page
    box = page.get_by_placeholder(CHAT_PLACEHOLDER)
    await d.type(box, question)
    await d.enter(box)
    prev_text, prev_step = d.last
    await d.caption(wait_caption, remember=False)
    await expect(page.locator(ANSWERS)).to_have_count(n_answers, timeout=120_000)
    await asyncio.sleep(1.3)
    await d.caption(prev_text, prev_step)  # ответ пришёл: возвращаем подпись текущего шага
    return page.locator(ANSWERS).nth(n_answers - 1)


async def scenario(d: Director) -> None:
    page = d.page

    async def go(name: str) -> None:
        """Переход по верхней навигации; отрисовывается только открытая страница."""
        link = page.locator('[data-testid="stTopNavLink"]').filter(has_text=name).first
        if await link.get_attribute("aria-current") != "page":
            await d.click(link)
            await asyncio.sleep(1.2)

    await d.card("<h1>Навигатор по регламентам МФТИ</h1><p>Порядок действий по внутренним документам со ссылкой на пункт</p>"
                 "<p class='small'>Кейс 4 · хакатон «ИИ в системе управления МФТИ» · база из 20 файлов организаторов</p>")
    await asyncio.sleep(4.2)
    await d.card(None)
    await asyncio.sleep(1.0)

    async def s_docs():
        await d.caption("База: 20 файлов организаторов дали 28 документов. Регламенты отделены от приказов, у каждого статус и утверждающий приказ", "1")
        await go("Документы")
        grid = page.locator('[data-testid="stDataFrame"]').first
        await d.point(grid, 0.35, 0.3)
        await asyncio.sleep(4.0)
    await d.step("документы", s_docs())

    async def s_ask1():
        await d.caption("Сотрудник выбирает роль и задаёт вопрос своими словами", "2")
        await go("Спросить")
        await d.click(page.get_by_role("button", name=re.compile("Роль:")).first)
        await d.click(page.get_by_role("combobox", name="Кто вы"))
        await d.click(page.get_by_role("option", name="работник (любой)"))
        await asyncio.sleep(0.8)
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        ans = await ask(d, "Как оформить служебную командировку?", 1)
        await d.caption("Краткий ответ и порядок действий: только из пунктов документа, под каждым шагом номер пункта", "3")
        await asyncio.sleep(3.2)
        await d.scroll_by(260, pause=2.4)
        await d.caption("Любой шаг проверяется по первоисточнику: текст пункта и страница оригинала с подсветкой", "4")
        cite = ans.locator(CITES).first
        await d.click(cite.locator("summary").first, fx=0.3)
        await asyncio.sleep(1.2)
        await d.click(cite.get_by_text("Показать страницу оригинала").first, fx=0.2)
        img = cite.locator("img").first
        await img.wait_for(state="visible", timeout=30_000)
        await asyncio.sleep(0.8)
        await d.scroll_to(img, offset=110, ms=1300, pause=1.0)
        await d.point(img, 0.5, 0.25)
        await asyncio.sleep(3.6)
    await d.step("командировка", s_ask1())

    async def s_ask2():
        await d.caption("Если релевантных документов несколько, ответ собирается из всех с указанием каждого источника", "5")
        await go("Спросить")
        ans = await ask(d, "Сколько составляют суточные при командировке по России по регламенту и по приказу о бюджете на 2026 год?", 2)
        await asyncio.sleep(2.4)
        warn = ans.locator('[data-testid="stAlert"]').first
        if await warn.count():
            await d.caption("Расхождение между документами показано сразу под кратким ответом: 100 ₽ в приказе и «не более 100 ₽» в регламенте", "5")
            await d.highlight(warn)
            await d.point(warn, 0.3, 0.5)
            await asyncio.sleep(4.0)
            await d.highlight(warn, False)
        await d.scroll_by(320, pause=2.0)
        checks = ans.locator('[data-testid="stExpander"]').filter(has_text="Проверки:").first
        await d.scroll_to(checks, offset=VH * 0.3, ms=1100)
        await d.caption("Проверки ответа: цитаты совпадают с текстом, документы действуют, редакция актуальна", "5")
        await d.click(checks.locator("summary").first, fx=0.3)
        await asyncio.sleep(0.6)
        await d.scroll_to(checks, offset=VH * 0.2, ms=900)
        await asyncio.sleep(4.0)
    await d.step("суточные", s_ask2())

    async def s_ask3():
        await d.caption("Учитывается действующая редакция: регламент открытия ФЛС 2021 года, а не 2017-го", "6")
        await go("Спросить")
        await ask(d, "Как открыть финансовый лицевой счёт для научно-исследовательской работы?", 3)
        await asyncio.sleep(3.2)
        await go("Документы")
        search = page.get_by_label("Поиск по названию")
        await d.type(search, "финансового лицевого")
        await d.enter(search)
        await asyncio.sleep(1.5)
        await d.caption("Редакция 2017 года (приказ № 460-1) помечена как предыдущая, действует редакция 2021 года (приказ № 3065-1)", "6")
        grid = page.locator('[data-testid="stDataFrame"]').first
        await d.point(grid, 0.12, 0.45)
        await asyncio.sleep(4.2)
        await go("Спросить")
    await d.step("ФЛС", s_ask3())

    async def s_ask4():
        await d.caption("Составной вопрос: система отвечает на обе части", "7")
        await go("Спросить")
        await ask(d, "Сколько длится внутренний заём и какой штраф, если не вернуть его вовремя?", 4)
        await asyncio.sleep(2.4)
        await d.scroll_by(240, pause=2.6)
    await d.step("заём", s_ask4())

    async def s_ask5():
        await d.caption("Вопрос об отменённом приказе: система знает, что он утратил силу, и называет основание", "8")
        await go("Спросить")
        await ask(d, "Что говорит приказ от 08.07.2020 № 1465-1 о внутреннем заимствовании?", 5)
        await asyncio.sleep(4.2)
    await d.step("отменённый приказ", s_ask5())

    async def s_ask6():
        await d.caption("Регламента нет в базе: честный отказ вместо домысла и список проверенных документов", "9")
        await go("Спросить")
        await ask(d, "Как оформить ежегодный оплачиваемый отпуск?", 6)
        await asyncio.sleep(4.2)
    await d.step("отказ", s_ask6())

    async def s_admin():
        await d.caption("Администратору: анализ влияния нового приказа до загрузки в базу", "10")
        await go("Администратору")
        await d.click(page.get_by_role("tab", name="Влияние приказа"))
        await asyncio.sleep(0.8)
        panel = page.get_by_role("tabpanel")  # видимая под-вкладка
        up = panel.locator('[data-testid="stFileUploader"]').first
        await d.point(up.locator('[data-testid="stFileUploaderDropzone"]'), 0.12, 0.5)
        await page.evaluate("a => window.__demo.tap(a[0], a[1])", [d.cx, d.cy])
        await up.locator('input[type="file"]').set_input_files("data/demo/org_prikaz_demo_izmeneniya_zaimstvovanie.txt")
        await d.caption("Демо-приказ меняет срок займа и размер штрафа: система находит регламент и показывает «было» и «стало»", "10")
        res = panel.get_by_text("Новых документов:", exact=False).first
        await res.wait_for(timeout=90_000)
        await asyncio.sleep(1.0)
        await d.scroll_to(res, offset=150, ms=1200)
        first = panel.locator('[data-testid="stExpander"]').first  # первые изменения раскрыты сразу
        await d.scroll_to(first, offset=VH * 0.3, ms=1100)
        await d.point(first, 0.3, 0.6)
        await asyncio.sleep(5.0)
    await d.step("анализ влияния", s_admin())

    async def s_quality():
        await d.caption("Качество измерено: 41 из 42 верных исходов на документах организаторов, ложных ответов нет", "11")
        await go("Качество")
        grid = page.locator('[data-testid="stDataFrame"]').first
        await d.point(grid, 0.4, 0.5)
        await asyncio.sleep(5.0)
    await d.step("качество", s_quality())

    await d.caption(None)
    await d.card("<h1>github.com/rn1nor/reglament-navigator</h1>"
                 "<p>Ответ только из пунктов документов · учёт редакций и отмен · честный отказ</p>"
                 "<p class='small'>docker compose up: система работает на документах организаторов</p>")
    await asyncio.sleep(5.0)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8601")
    ap.add_argument("--out", default="demo/navigator_demo.mp4")
    ap.add_argument("--channel", default="chrome", help="chrome — установленный Google Chrome; пусто — Chromium из playwright install")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("нужен ffmpeg: pip install imageio-ffmpeg", file=sys.stderr)
        return 1
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel=args.channel or None, headless=not args.headed)
        # прогрев: модели поиска загружаются один раз на сервере, в записи ответы идут без задержки
        warm = await browser.new_page(viewport={"width": VW, "height": VH})
        await warm.goto(args.url, wait_until="domcontentloaded")
        box = warm.get_by_placeholder(CHAT_PLACEHOLDER)
        await box.wait_for(timeout=180_000)
        await box.fill("Как оформить служебную командировку?")
        await box.press("Enter")
        await expect(warm.locator(ANSWERS)).to_have_count(1, timeout=180_000)
        await warm.close()

        ctx = await browser.new_context(viewport={"width": VW, "height": VH}, device_scale_factor=DSF, locale="ru-RU")
        page = await ctx.new_page()
        await page.goto(args.url, wait_until="domcontentloaded")
        await page.get_by_placeholder(CHAT_PLACEHOLDER).wait_for(timeout=120_000)
        await asyncio.sleep(2.0)
        d = Director(page)
        await d.ensure()
        with tempfile.TemporaryDirectory(prefix="demo_frames_") as tmp:
            rec = Recorder(page, Path(tmp))
            await rec.start()
            await asyncio.sleep(0.5)
            await scenario(d)
            await rec.stop()
            print(f"кадров: {len(rec.frames)}; сборка видео…")
            rec.encode(Path(args.out), ffmpeg)
        await browser.close()
    print(f"готово: {args.out}")
    if d.failures:
        print("не удались шаги:\n  " + "\n  ".join(d.failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
