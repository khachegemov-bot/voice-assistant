"""Собирает «Инструкция.html» — одна страница со встроенными снимками экрана (открывается в любом браузере).
Запуск: venv\\Scripts\\python tools\\make_guide.py <папка со снимками> <куда сохранить .html>"""
import base64
import io
import sys
from pathlib import Path

from PIL import Image

SHOTS, OUT = Path(sys.argv[1]), Path(sys.argv[2])


def img(name: str, width: int = 520, alt: str = "") -> str:
    im = Image.open(SHOTS / f"{name}.png")
    if im.mode == "RGBA":                                  # окошки с прозрачной тенью — кладём на фон страницы
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        im = bg
    im = im.convert("RGB")
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88, optimize=True)
    data = base64.b64encode(buf.getvalue()).decode()
    return f'<img src="data:image/jpeg;base64,{data}" alt="{alt}" width="{im.width}">'


HTML = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Голосовой помощник — инструкция</title>
<style>
  :root {{ --bg:#f6f6fb; --card:#fff; --text:#1d1f27; --muted:#626877; --accent:#6b5ae6; --soft:#efedfd;
          --border:#e6e6ef; --ok:#14a06f; --warn:#c2410c; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text); font:16px/1.6 "Segoe UI", system-ui, sans-serif; }}
  main {{ max-width:860px; margin:0 auto; padding:32px 20px 64px; }}
  header {{ background:linear-gradient(135deg,#7c6cf2,#5b8cff); color:#fff; border-radius:22px; padding:34px 32px; }}
  header h1 {{ margin:0 0 6px; font-size:32px; }}
  header p {{ margin:0; opacity:.92; font-size:17px; }}
  .keys {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:20px; }}
  .key {{ background:rgba(255,255,255,.18); border-radius:12px; padding:8px 14px; font-weight:600; }}
  section {{ background:var(--card); border:1px solid var(--border); border-radius:18px; padding:26px 28px;
            margin-top:20px; }}
  h2 {{ margin:0 0 12px; font-size:22px; display:flex; align-items:center; gap:10px; }}
  h2 .n {{ background:var(--soft); color:var(--accent); border-radius:10px; width:34px; height:34px;
          display:inline-flex; align-items:center; justify-content:center; font-size:17px; }}
  h3 {{ margin:18px 0 6px; font-size:17px; }}
  kbd {{ background:var(--soft); color:var(--accent); border-radius:6px; padding:1px 7px; font:600 14px "Segoe UI"; }}
  .row {{ display:flex; gap:24px; align-items:flex-start; flex-wrap:wrap; }}
  .row > div {{ flex:1 1 300px; }}
  img {{ max-width:100%; height:auto; border-radius:14px; box-shadow:0 8px 30px rgba(20,20,40,.18); }}
  .shot {{ text-align:center; margin:14px 0 4px; }}
  .cap {{ color:var(--muted); font-size:13px; text-align:center; margin-top:6px; }}
  .tip {{ background:var(--soft); border-radius:12px; padding:12px 16px; margin-top:14px; }}
  .warn {{ background:#fff4ec; color:#7c2d12; border-radius:12px; padding:12px 16px; margin-top:14px; }}
  ol, ul {{ padding-left:22px; margin:6px 0; }}
  li {{ margin:4px 0; }}
  table {{ width:100%; border-collapse:collapse; margin-top:8px; font-size:15px; }}
  td, th {{ border-bottom:1px solid var(--border); padding:9px 8px; text-align:left; vertical-align:top; }}
  th {{ color:var(--muted); font-weight:600; }}
  footer {{ color:var(--muted); font-size:13px; text-align:center; margin-top:28px; }}
</style></head><body><main>

<header>
  <h1>🎙 Голосовой помощник</h1>
  <p>Пишет текст под диктовку и переводит английские лекции и подкасты на русский. Распознавание речи работает
     прямо на компьютере — без интернета и без подписок.</p>
  <div class="keys"><span class="key">Alt+Z — диктовка</span><span class="key">Alt+X — лекция / подкаст</span></div>
</header>

<section>
  <h2><span class="n">1</span>Первый запуск</h2>
  <ol>
    <li>Распакуйте архив в любую папку (например, «Документы»). <b>Не запускайте прямо из архива.</b></li>
    <li>Откройте <b>«Голосовой помощник.exe»</b>. Первый запуск занимает 10–20 секунд.</li>
    <li>Если Windows покажет «Система Windows защитила ваш компьютер» — нажмите <b>«Подробнее» →
        «Выполнить в любом случае»</b>. Это обычное предупреждение для программ без цифровой подписи.</li>
    <li>Чтобы программа запускалась сама — в разделе <b>«Настройки»</b> включите <b>«Запускать вместе с Windows»</b>.</li>
  </ol>
  <div class="tip">💡 Крестик ✕ не выключает программу — она прячется к часам (значок с микрофоном) и продолжает
    слушать горячие клавиши. Полностью выйти: правый клик по значку у часов → <b>«Выход»</b>.</div>
</section>

<section>
  <h2><span class="n">2</span>Диктовка — <kbd>Alt+Z</kbd></h2>
  <div class="row">
    <div>
      <ol>
        <li>Поставьте курсор туда, где нужен текст: Word, браузер, мессенджер, почта…</li>
        <li>Нажмите <kbd>Alt+Z</kbd> (или большую кнопку с микрофоном на «Главной») — прозвучит мягкий сигнал,
          внизу экрана появится окошко.</li>
        <li>Говорите. Слова плавно появляются в окошке по ходу речи, столбики справа показывают, что вас слышно.</li>
        <li>Ещё раз <kbd>Alt+Z</kbd> — текст вставится туда, где стоит курсор. Пока вы говорите, программа
          в фоне перепроверяет запись целыми фразами, поэтому вставляется уже исправленный текст.</li>
      </ol>
      <h3>Язык</h3>
      «Авто» — язык определяется по голосу: говорите по-русски или по-английски, раскладку переключать
      не нужно. Можно выбрать и вручную — на «Главной».
      <h3>Команды голосом</h3>
      «новая строка», «новый абзац» · по-английски «new line», «new paragraph».
    </div>
    <div class="shot">{img("1_dictation_tab", 360, "Главная")}<div class="cap">«Главная»: кнопка диктовки,
      статистика, язык и последняя диктовка</div></div>
  </div>
  <div class="shot">{img("7_bubble_dictation", 620, "Окошко диктовки")}<div class="cap">Окошко во время диктовки: белым —
    уже записанное, серым — ещё уточняется, синие столбики — громкость вашего голоса</div></div>
</section>

<section>
  <h2><span class="n">3</span>Лекции и подкасты — <kbd>Alt+X</kbd></h2>
  <div class="row">
    <div>
      <p>В разделе <b>«Лекции»</b> выберите, откуда слушать:</p>
      <table>
        <tr><th>Источник</th><th>Когда выбирать</th></tr>
        <tr><td><b>Весь звук</b></td><td>Слушает всё, что играет на компьютере.</td></tr>
        <tr><td><b>Программа</b></td><td>Только одно приложение — Zoom, Teams, браузер. Можно параллельно смотреть
          своё видео <b>в другой программе</b> (например, лекция в Edge, видео в Firefox). Нажмите ↻, чтобы
          обновить список.</td></tr>
        <tr><td><b>Ссылка</b></td><td>Ссылка на YouTube, VK Видео, Twitch. Звук у вас вообще не играет. Запись
          обрабатывается быстрее реального времени (час лекции ≈ 10–15 минут), прямой эфир — по ходу.</td></tr>
      </table>
      <ol style="margin-top:12px">
        <li>Выберите источник и язык лекции.</li>
        <li><kbd>Alt+X</kbd> (или большая кнопка ▶) — начать. Английский текст появляется в окне внизу экрана.</li>
        <li>Ещё раз <kbd>Alt+X</kbd> — остановить: появится перевод всего текста и оригинал.</li>
      </ol>
    </div>
    <div class="shot">{img("2_lectures_tab", 360, "Лекции")}<div class="cap">Раздел «Лекции»</div></div>
  </div>
  <div class="tip">🔇 «Приглушить её для меня» (режим «Программа») — лекция почти не слышна вам, но записывается
    полностью. После остановки громкость вернётся сама.</div>
  <div class="shot">{img("8_bubble_lecture", 660, "Запись лекции")}<div class="cap">Во время записи: текст
    по ходу речи</div></div>
  <div class="shot">{img("9_bubble_translation", 660, "Перевод")}<div class="cap">После остановки: перевод, оригинал,
    кнопки «Копировать перевод» и «Открыть файл»</div></div>
  <p>Каждая лекция сохраняется в папку <b>Документы → Голосовой помощник → Лекции</b>.</p>
</section>

<section>
  <h2><span class="n">4</span>Перевод: онлайн или офлайн</h2>
  <table>
    <tr><th></th><th>Онлайн · Google</th><th>Офлайн</th></tr>
    <tr><td>Качество</td><td>живой, естественный перевод</td><td>верный по смыслу, иногда буквальный</td></tr>
    <tr><td>Интернет</td><td>нужен</td><td>не нужен</td></tr>
    <tr><td>Приватность</td><td>английский текст уходит в Google</td><td>всё остаётся на компьютере</td></tr>
  </table>
  <p>Если интернет пропадёт, программа сама переключится на офлайн-перевод.</p>
</section>

<section>
  <h2><span class="n">5</span>История</h2>
  <div class="row">
    <div>В разделе «История» — последние 10 диктовок: их можно найти поиском, скопировать или удалить.
      Более старые удаляются сами.</div>
    <div class="shot">{img("3_history_tab", 360, "История")}<div class="cap">Раздел «История»</div></div>
  </div>
</section>

<section>
  <h2><span class="n">6</span>Настройки</h2>
  <div class="row">
    <div>Микрофон (если есть гарнитура — её микрофон обычно слышит чище), вставка текста, окошко,
      автозапуск с Windows. «Хранить 5 последних записей» — записи остаются только на этом компьютере,
      чтобы при необходимости проверить, как программа слышит голос.</div>
    <div class="shot">{img("4_settings_tab", 360, "Настройки")}<div class="cap">Раздел «Настройки»</div></div>
  </div>
</section>

<section>
  <h2><span class="n">7</span>Если что-то не работает</h2>
  <ul>
    <li><b>Окошко пишет «Микрофон выключен»</b> — включите микрофон: клавиша с перечёркнутым микрофоном
      на ноутбуке или <i>Параметры → Система → Звук → Ввод</i>.</li>
    <li><b>«Alt+Z / Alt+X занята другой программой»</b> — закройте другие программы для диктовки
      и перезапустите помощник.</li>
    <li><b>Программы нет в списке «Программа»</b> — включите в ней звук (запустите видео) и нажмите ↻.</li>
    <li><b>Ссылка перестала работать</b> — YouTube периодически меняет защиту; нужна новая версия программы.</li>
    <li><b>Журнал работы</b> (можно прислать автору): <code>%LOCALAPPDATA%\\Голосовой помощник\\log.txt</code>
      — вставьте этот путь в адресную строку Проводника.</li>
  </ul>
  <div class="warn">Работает на Windows 10 и 11. На слабых компьютерах распознавание медленнее.</div>
</section>

<footer>Модели: GigaAM v3 (Сбер, MIT) · NVIDIA Parakeet TDT 0.6B v2 (CC-BY-4.0) ·
  OPUS-MT tc-big (Хельсинкский университет, CC-BY-4.0) · Silero VAD (MIT)</footer>
</main></body></html>
"""
OUT.write_text(HTML, "utf-8")
print(f"инструкция: {OUT}  ({OUT.stat().st_size / 1024:.0f} КБ)")
