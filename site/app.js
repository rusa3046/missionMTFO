/* ---------------------------------------------------------------------------
   Renders the page from the RESUME object in resume-data.js.

   Everything is built with createElement rather than innerHTML, so text from
   the data file is never parsed as markup — an ampersand or a "<" in a job
   title just renders as itself.
   ------------------------------------------------------------------------- */

(function () {
  "use strict";

  // --- Tiny DOM helper ------------------------------------------------------

  function el(tag, props, children) {
    var node = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (key) {
        if (props[key] === null || props[key] === undefined) return;
        if (key === "class") node.className = props[key];
        else if (key === "text") node.textContent = props[key];
        else node.setAttribute(key, props[key]);
      });
    }
    (children || []).forEach(function (child) {
      if (child) node.appendChild(child);
    });
    return node;
  }

  function has(value) {
    return typeof value === "string" && value.trim() !== "";
  }

  function nonEmpty(list) {
    return Array.isArray(list) && list.length > 0;
  }

  function section(title, blocks) {
    return el("section", null, [
      el("h2", { class: "section-title", text: title }),
    ].concat(blocks));
  }

  function bulletList(bullets) {
    if (!nonEmpty(bullets)) return null;
    return el(
      "ul",
      null,
      bullets.map(function (b) {
        return el("li", { text: b });
      })
    );
  }

  function chipList(items) {
    if (!nonEmpty(items)) return null;
    return el(
      "ul",
      { class: "chips" },
      items.map(function (item) {
        return el("li", { class: "chip", text: item });
      })
    );
  }

  function dateRange(start, end) {
    if (has(start) && has(end)) return start + " — " + end;
    return has(start) ? start : has(end) ? end : "";
  }

  // --- Header ---------------------------------------------------------------

  function renderMasthead(data) {
    var meta = [];

    if (has(data.location)) {
      meta.push(el("span", { class: "location", text: data.location }));
    }
    if (has(data.email)) {
      meta.push(
        el("a", {
          class: "btn btn--primary",
          href: "mailto:" + data.email,
          text: data.email,
        })
      );
    }
    (data.links || []).forEach(function (link) {
      if (!has(link.href) || !has(link.label)) return;
      meta.push(
        el("a", {
          class: "btn",
          href: link.href,
          rel: "noopener",
          target: "_blank",
          text: link.label,
        })
      );
    });
    if (has(data.resumePdf)) {
      meta.push(
        el("a", {
          class: "btn",
          href: data.resumePdf,
          "data-print-hide": "",
          text: "Résumé (PDF)",
        })
      );
    }

    return el("header", { class: "masthead" }, [
      el("h1", { text: data.name }),
      has(data.tagline) ? el("p", { class: "tagline", text: data.tagline }) : null,
      meta.length ? el("div", { class: "meta" }, meta) : null,
    ]);
  }

  // --- Entries --------------------------------------------------------------

  function renderExperience(job) {
    var head = el("div", { class: "entry-head" }, [
      el("h3", { class: "entry-title" }, [
        document.createTextNode(job.role || ""),
        has(job.org)
          ? el("span", { class: "entry-org", text: "  ·  " + job.org })
          : null,
      ]),
      el("span", { class: "entry-dates", text: dateRange(job.start, job.end) }),
    ]);

    return el("article", { class: "entry" }, [
      head,
      has(job.location) ? el("p", { class: "entry-sub", text: job.location }) : null,
      bulletList(job.bullets),
      chipList(job.tech),
    ]);
  }

  function renderProject(project) {
    var links = [];
    if (has(project.href)) {
      links.push(
        el("a", { href: project.href, rel: "noopener", target: "_blank", text: "Live" })
      );
    }
    if (has(project.repo)) {
      links.push(
        el("a", { href: project.repo, rel: "noopener", target: "_blank", text: "Code" })
      );
    }

    return el("article", { class: "card" }, [
      el("div", { class: "entry-head" }, [
        el("h3", { class: "entry-title", text: project.name }),
        links.length ? el("div", { class: "card-links" }, links) : null,
      ]),
      has(project.blurb)
        ? el("p", { class: "card-blurb", text: project.blurb })
        : null,
      bulletList(project.bullets),
      chipList(project.tech),
    ]);
  }

  function renderEducation(school) {
    return el("article", { class: "entry" }, [
      el("div", { class: "entry-head" }, [
        el("h3", { class: "entry-title" }, [
          document.createTextNode(school.degree || school.school || ""),
          has(school.degree) && has(school.school)
            ? el("span", { class: "entry-org", text: "  ·  " + school.school })
            : null,
        ]),
        el("span", {
          class: "entry-dates",
          text: dateRange(school.start, school.end),
        }),
      ]),
      has(school.location)
        ? el("p", { class: "entry-sub", text: school.location })
        : null,
      has(school.detail) ? el("p", { class: "entry-sub", text: school.detail }) : null,
    ]);
  }

  function renderSkills(groups) {
    var cells = [];
    groups.forEach(function (group) {
      if (!nonEmpty(group.items)) return;
      cells.push(el("p", { class: "skill-group", text: group.group }));
      cells.push(chipList(group.items));
    });
    return el("div", { class: "skills" }, cells);
  }

  // --- Theme ----------------------------------------------------------------

  function setupTheme() {
    var root = document.documentElement;
    var button = document.querySelector(".theme-toggle");
    var stored = null;

    try {
      stored = localStorage.getItem("theme");
    } catch (err) {
      /* private browsing — fall back to the system preference */
    }
    if (stored === "light" || stored === "dark") root.setAttribute("data-theme", stored);

    function current() {
      var explicit = root.getAttribute("data-theme");
      if (explicit) return explicit;
      return window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
    }

    function paint() {
      var dark = current() === "dark";
      button.textContent = dark ? "☀" : "☾";
      button.setAttribute(
        "aria-label",
        dark ? "Switch to light theme" : "Switch to dark theme"
      );
    }

    button.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try {
        localStorage.setItem("theme", next);
      } catch (err) {
        /* nothing to do — the toggle still works for this page view */
      }
      paint();
    });

    paint();
  }

  // --- Boot -----------------------------------------------------------------

  function render() {
    // resume-data.js declares RESUME with `const`, which is scoped to the
    // script rather than hung off window — so read the binding, not a property.
    var data = typeof RESUME !== "undefined" ? RESUME : null;
    var root = document.getElementById("app");

    if (!data) {
      root.appendChild(
        el("p", { text: "resume-data.js did not load — check the file path." })
      );
      return;
    }

    document.title = data.name + " — " + (data.tagline || "Résumé");
    var description = document.querySelector('meta[name="description"]');
    if (description && has(data.summary)) {
      description.setAttribute("content", data.summary.slice(0, 160));
    }

    root.appendChild(renderMasthead(data));

    var main = el("main", null, []);

    if (has(data.summary)) {
      main.appendChild(section("About", [el("p", { class: "summary", text: data.summary })]));
    }
    if (nonEmpty(data.experience)) {
      main.appendChild(section("Experience", data.experience.map(renderExperience)));
    }
    if (nonEmpty(data.projects)) {
      main.appendChild(
        section("Projects", [
          el("div", { class: "projects" }, data.projects.map(renderProject)),
        ])
      );
    }
    if (nonEmpty(data.skills)) {
      main.appendChild(section("Skills", [renderSkills(data.skills)]));
    }
    if (nonEmpty(data.education)) {
      main.appendChild(section("Education", data.education.map(renderEducation)));
    }

    root.appendChild(main);

    var footerBits = [el("span", { text: data.name })];
    if (has(data.footerNote)) {
      footerBits.push(el("span", { text: data.footerNote }));
    } else if (has(data.email)) {
      footerBits.push(
        el("a", { href: "mailto:" + data.email, text: data.email })
      );
    }
    root.appendChild(el("footer", null, footerBits));
  }

  render();
  setupTheme();
})();
