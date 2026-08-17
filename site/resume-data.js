/* ---------------------------------------------------------------------------
   THIS IS THE ONLY FILE YOU NEED TO EDIT.

   Everything on the site is generated from the object below. Replace the
   placeholder text, save, refresh the browser. Nothing else has to change.

   Rules the renderer follows:
     - Any section left as an empty array ([]) is hidden entirely.
     - Any field left as an empty string ("") is skipped, not printed blank.
     - `end: "Present"` renders as a current role.
   ------------------------------------------------------------------------- */

const RESUME = {
  // --- Identity -----------------------------------------------------------
  name: "First Last",
  // One line, under ~60 characters. What she does, not what she wants.
  tagline: "Software engineer — backend, platform, and infrastructure",
  location: "Seattle, WA",
  email: "first.last@example.com",

  // Optional: drop a PDF at site/resume.pdf and this button appears.
  // Set to "" to hide the download button.
  resumePdf: "resume.pdf",

  // Shown as buttons in the header. Delete any you don't want.
  links: [
    { label: "GitHub", href: "https://github.com/USERNAME" },
    { label: "LinkedIn", href: "https://www.linkedin.com/in/USERNAME" },
  ],

  // --- Summary ------------------------------------------------------------
  // Two or three sentences. Concrete beats aspirational: what she has built,
  // in what domain, at what scale.
  summary:
    "Placeholder. Two or three sentences about what she builds and where. " +
    "Lead with the most specific true thing — a system she owned, a scale " +
    "number, a language she is genuinely deep in — and cut anything that " +
    "would be true of any other new graduate.",

  // --- Experience ---------------------------------------------------------
  // Most recent first. Bullets should start with a verb and, wherever the
  // number exists, carry one.
  experience: [
    {
      role: "Software Engineer Intern",
      org: "Company Name",
      location: "Seattle, WA",
      start: "Jun 2025",
      end: "Sep 2025",
      bullets: [
        "Built X that did Y, cutting Z by N%.",
        "Shipped the service to production and owned it on-call for the last four weeks.",
        "Wrote the migration that moved N million rows with no downtime.",
      ],
      tech: ["Python", "PostgreSQL", "AWS"],
    },
    {
      role: "Undergraduate Research Assistant",
      org: "Lab or Department Name",
      location: "City, ST",
      start: "Jan 2025",
      end: "May 2025",
      bullets: [
        "One line on what the research was actually for.",
        "One line on what she personally built or measured.",
      ],
      tech: ["PyTorch", "CUDA"],
    },
  ],

  // --- Projects -----------------------------------------------------------
  // Two to four. Prefer things that run over things that were assignments.
  projects: [
    {
      name: "Project Name",
      blurb: "One sentence on what it does and who it is for.",
      bullets: [
        "The interesting engineering decision, and why it went that way.",
        "The result — users, throughput, latency, or what it replaced.",
      ],
      tech: ["TypeScript", "React", "SQLite"],
      href: "",                                  // live demo, or "" to omit
      repo: "https://github.com/USERNAME/repo",  // or "" to omit
    },
    {
      name: "Second Project",
      blurb: "One sentence.",
      bullets: ["What made it non-trivial."],
      tech: ["Go", "Redis"],
      href: "",
      repo: "",
    },
  ],

  // --- Skills -------------------------------------------------------------
  // Group them. Inside each group, strongest first — people read three items
  // and stop. Leave out anything she would not want to be interviewed on.
  skills: [
    { group: "Languages", items: ["Python", "TypeScript", "Go", "SQL", "Java"] },
    { group: "Infrastructure", items: ["AWS", "Docker", "Kubernetes", "Terraform"] },
    { group: "Data", items: ["PostgreSQL", "Redis", "Kafka"] },
    { group: "Tools", items: ["Git", "GitHub Actions", "Linux"] },
  ],

  // --- Education ----------------------------------------------------------
  education: [
    {
      school: "University Name",
      degree: "B.S. in Computer Science",
      location: "City, ST",
      start: "2022",
      end: "2026",
      detail: "Relevant coursework, honors, or GPA — or delete this line.",
    },
  ],

  // --- Footer -------------------------------------------------------------
  // Shown small at the bottom. Set to "" to hide.
  footerNote: "",
};
