# briq.ie

The public legal site for the Bríq app: the privacy policy, the terms of
service, and the account deletion instructions that the App Store and the Play
Console point at.

It is hand written static HTML. There is no generator, no build step and no
dependencies. `.nojekyll` switches off Jekyll processing so the files are served
exactly as they are committed, and `CNAME` binds the custom domain.

| Page | File |
| --- | --- |
| https://briq.ie/ | `index.html` |
| https://briq.ie/privacy/ | `privacy/index.html` |
| https://briq.ie/terms/ | `terms/index.html` |
| https://briq.ie/delete-account/ | `delete-account/index.html` |
| any unknown path | `404.html` |

## Publishing

**A push to `main` publishes the site.** GitHub Pages serves this branch from
its root, so whatever lands on `main` is live within a minute or two. There is
no staging step and no deploy workflow to approve, so review before pushing,
not after.

## The legal text must match the app

These pages are not written here. They are a transcription of what the app
shows its users, and the app is the source of truth:

- `app/privacy-policy.tsx` in the app repository, for the privacy policy
- `app/terms-of-service.tsx` in the app repository, for the terms of service

When a passage differs, the site takes the app's sentence word for word. Do not
compose new legal wording on this side. A site that promises something the code
does not do, or that hides something the code does, is a GDPR Article 13
problem rather than a copy problem, which is why the recipient list, the
retention periods and the in-app navigation paths are checked mechanically.

The app's own copy carries no em or en dashes, so neither does this site.
"Bríq" always keeps its fada outside domains, URLs and email addresses.

## Running the checks locally

Python 3 and the standard library, nothing to install:

```
python tools/check-site.py
```

It exits non-zero and prints `file:line: reason` for every failure. It checks
every `.html` file in the repository for dashes, a missing fada, a list of
banned claims and stale screen names, internal links that resolve, one skip
link and one main landmark per page, matching tags, the printed dates, and that
section 6 of the privacy policy names exactly the app's nine processors. To
check another copy of the tree, pass its folder as an argument.

The same script runs in CI on every push and pull request to `main`, from
`.github/workflows/check-site.yml`.
