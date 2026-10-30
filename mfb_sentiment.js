// myfxbook sentiment scraper for GitHub Actions (puppeteer-core + system Chrome).
// Outputs JSON array to stdout: [{symbol, short_pct, long_pct, short_lots, long_lots, total_lots}]
const puppeteer = require('puppeteer-core');

(async () => {
  const exe = process.env.CHROME_PATH || '/usr/bin/google-chrome';
  const browser = await puppeteer.launch({
    executablePath: exe,
    headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled'],
    defaultViewport: { width: 1600, height: 2400 },
  });
  const page = await browser.newPage();
  await page.setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36');
  await page.goto('https://www.myfxbook.com/community/outlook', { waitUntil: 'networkidle2', timeout: 90000 }).catch(() => {});
  await new Promise(r => setTimeout(r, 12000));
  const rows = await page.evaluate(() => {
    const out = [];
    const table = document.querySelector('table');
    if (!table) return out;
    for (const tr of table.querySelectorAll('tr')) {
      const cells = [...tr.querySelectorAll('td,th')].map(td => td.innerText.trim());
      if (cells.length < 3) continue;
      const sym = cells[0];
      if (!/^[A-Z]{6}$/.test(sym)) continue;
      const text = cells.join(' ');
      const shortM = text.match(/Short\s+(\d+(?:\.\d+)?)\s*%[^|]*?([\d,\.]+)\s*lots/);
      const longM = text.match(/Long\s+(\d+(?:\.\d+)?)\s*%[^|]*?([\d,\.]+)\s*lots/);
      if (!shortM || !longM) continue;
      const sl = parseFloat(shortM[2].replace(/,/g, ''));
      const ll = parseFloat(longM[2].replace(/,/g, ''));
      out.push({
        symbol: sym,
        short_pct: parseFloat(shortM[1]),
        long_pct: parseFloat(longM[1]),
        short_lots: sl,
        long_lots: ll,
        total_lots: Math.round((sl + ll) * 100) / 100,
      });
    }
    return out;
  });
  console.log(JSON.stringify(rows));
  await browser.close();
})().catch(e => { console.error('mfb fatal', e.message); process.exit(1); });
