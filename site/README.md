# Résumé site

A single-page personal site. No build step, no framework, no dependencies —
three files and a browser.

```
site/
  index.html        page shell (rarely needs touching)
  resume-data.js    ← all the content lives here
  app.js            renders the page from resume-data.js
  styles.css        colors, type, layout, print rules
```

## Editing the content

Open `resume-data.js` and replace the placeholders. That is the whole workflow —
`app.js` rebuilds the page from that object every load.

- An empty array (`[]`) hides its section completely. No projects yet? Set
  `projects: []` and the Projects heading disappears.
- An empty string (`""`) hides that one field rather than printing a blank line.
- `end: "Present"` reads as a current role.
- Text is inserted as text, never as HTML, so apostrophes, `&`, and `<` are safe
  to type literally.

## Previewing it locally

```sh
cd site
python3 -m http.server 8000
```

Then open <http://localhost:8000>. Opening `index.html` straight off the disk
works in most browsers too.

## Getting a PDF

Print the page (Cmd/Ctrl-P) and save as PDF. The print stylesheet drops the
theme toggle, the card borders, and the project links, and tightens the type —
what comes out is a plain, ATS-friendly, black-on-white résumé rather than a
screenshot of a website.

If you would rather link a PDF she already has, drop it at `site/resume.pdf` and
leave `resumePdf: "resume.pdf"` set; the header button will find it. Set that
field to `""` to remove the button.

## Theme

Follows the visitor's system light/dark setting, with a toggle in the corner
that remembers the choice in `localStorage`.

## Publishing it

Any static host will serve this directory as-is. For GitHub Pages:

1. **Settings → Pages → Source: Deploy from a branch**
2. Pick the branch, and set the folder to `/site` if the option is offered —
   otherwise move these files to the repo root or a `/docs` directory, which are
   the two folders Pages will serve from.

Note that this repository is public, so anything in `resume-data.js` is public
too. Use an email address she is happy to publish, or drop the `email` field and
keep contact to LinkedIn.
