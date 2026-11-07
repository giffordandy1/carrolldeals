# Carroll Deals

A free map of local events and specials. GitHub checks the websites you list every 6 hours and publishes the results as a web page. There's no server to pay for or keep running.

- **The map page** lets visitors use their location or type a ZIP code or city, then filter by distance, events or specials, date range and keyword.
- **The site list** lives in [`sources.yml`](sources.yml). Edit it on GitHub to add, pause or remove sites.

## One-time setup (about 5 minutes)

1. **Turn on the web page.** In this repo, go to **Settings → Pages**. Under *Build and deployment*, set *Source* to **Deploy from a branch**, branch **main**, folder **/docs**, then **Save**. After a minute or two the page shows your site's address, like `https://<your-username>.github.io/<repo-name>/`.
2. **Let the updater save results.** Go to **Settings → Actions → General**, scroll to *Workflow permissions*, choose **Read and write permissions**, then **Save**.
3. **Add your sites.** Open `sources.yml`, click the pencil icon, add your sites (the file explains the format), then **Commit changes**. Saving starts a check right away.
4. **Watch it run.** The **Actions** tab shows each check. Click a run, then **Check every site**, to see what was found on each site and any problems.

You can start a check anytime from **Actions → Update events and specials → Run workflow**.

## Adding a site

```yaml
sites:
  - name: Main Street Tavern
    address: 123 Main St, Westminster, MD 21157
    urls:
      - https://www.example.com/events
      - https://www.example.com/specials
```

Indentation matters: use spaces, lined up as shown. If a check fails right after you edit the file, the Actions log usually points to the line with the mistake.

**Getting good results**
- Point at the specific *Events*, *Specials* or *Happy Hour* page, not the homepage.
- After a check, open the run log on the Actions tab to see how many items each site gave. `docs/data/items.json` has the full results.
- If a site gives junk or nothing, add `selectors` (the comments in `sources.yml` explain them). In your browser, right-click an event on that site and choose *Inspect* to find its class name.
- Pages that only show content after JavaScript runs (Facebook, Instagram, some Wix/Squarespace widgets) can't be read. Look for the venue's plain events page or its Eventbrite/ticketing page instead.
- Some sites block requests from cloud servers like GitHub's. Those show an error such as `403` in the log.
- For a site that blocks the checker or only posts on Facebook, type its standing events and specials in by hand with `manual:` (see Tony Locos in `sources.yml` for an example). You can list `urls` and `manual` items for the same site.

## How it reads a page

1. **Structured event data** (schema.org `Event`) that most ticketing and many venue sites publish. This is the most reliable.
2. **Your selectors**, if you set them for that site.
3. **Pattern matching** for happy hours, specials, deals, prices, trivia, live music and similar. Days and times ("Mon–Fri 4–7pm") become recurring specials; calendar dates ("Nov 14") become events.

Each item is placed at its own address if the page lists one, otherwise at the site's address. Addresses are looked up with OpenStreetMap and saved in `scraper/geocache.json`, so each one is only looked up once.

## Sharing

- The **Share** button opens the phone's share menu (or copies the link on a computer). With a place picked, it shares a link straight to that place.
- Each card has its own **Share** link for sending one deal to a friend.
- `docs/share-card.png` is the preview picture that shows when the link is pasted into a text, Facebook, etc.
- `docs/qr-code.png` is a printable QR code that opens the site.
- If you move to your own domain, update the two `og:` addresses near the top of `docs/index.html` and make a new QR code.

## Logo

The Carroll Deals logo is a price tag with a map pin punched into it: deals, near you. Files are in `brand/`:
`carroll-deals-logo.png` (logo with name, transparent background), `carroll-deals-mark.svg` (symbol only, any size)
and `carroll-deals-app-icon.svg` (symbol on a dark square). Colors: navy `#1E2A3A`, yellow `#F2B705`.

## Good to know

- It costs nothing as long as the repo is **public**. That also means the repo, including your site list, is visible to anyone.
- The checker respects each site's `robots.txt`, identifies itself, and waits between pages on the same site.
- GitHub may delay scheduled runs by a few minutes at busy times.
- To change how often it checks, edit the `cron` line in `.github/workflows/update.yml`. Times are in UTC.

## Try it on your computer (optional)

```bash
pip install -r requirements.txt
python -m scraper.run                 # checks the sites, writes docs/data/items.json
cd docs && python -m http.server 8000 # then open http://localhost:8000
```

## Files

```
sources.yml                     your site list
scraper/extract.py              reads events and specials out of a page
scraper/run.py                  checks every site and writes the results
scraper/geocache.json           saved address lookups
docs/index.html                 the map page
docs/data/items.json            latest results (updated automatically)
docs/data/status.json           how each site did on the last check
.github/workflows/update.yml    the schedule
```

Map data © OpenStreetMap contributors. Map library: Leaflet (BSD-2-Clause).
