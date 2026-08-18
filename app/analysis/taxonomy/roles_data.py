"""Domain and role registry.

Data only - no logic.

CLAUDE.md section 3 is the governing rule here: the system detects exactly one
domain and one target role, and may only search *same-role* title variations.

That constraint is enforced structurally by this file. A role's ``variations``
are the ONLY titles ever searched for that role, and each list contains
nothing but re-phrasings of the same job. Adjacent-but-different jobs are
separate ``RoleEntry`` rows precisely so they can never leak into one another's
variations - which is why Financial Analyst, Accountant, Auditor, Business
Analyst, Data Analyst, Finance Manager and Financial Advisor all exist as
distinct roles.

Never add a different job to a role's ``variations`` because it "seems close".
"""

from __future__ import annotations

# --- Domains ---------------------------------------------------------------
# (key, display_name, keyword phrases that signal this domain)
DOMAIN_ENTRIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("software_engineering", "Software Engineering",
     ("software development", "software engineering", "web development",
      "application development", "backend", "frontend", "full stack",
      "code review", "software design", "api development")),
    ("data", "Data & Analytics",
     ("data analysis", "data analytics", "data science", "data pipeline",
      "business intelligence", "reporting and analytics", "dashboards",
      "data engineering", "analytics")),
    ("devops_cloud", "DevOps & Cloud",
     ("devops", "site reliability", "cloud infrastructure", "deployment pipeline",
      "infrastructure", "observability", "platform engineering")),
    ("cybersecurity", "Cybersecurity",
     ("cybersecurity", "information security", "security operations",
      "threat detection", "security compliance", "network security")),
    ("finance", "Finance",
     ("financial planning", "financial analysis", "corporate finance",
      "investment analysis", "financial forecasting", "capital markets",
      "treasury", "portfolio analysis")),
    ("accounting", "Accounting",
     ("accounting", "bookkeeping", "general ledger", "month-end close",
      "financial statements", "tax return", "audit engagement")),
    ("marketing", "Marketing",
     ("marketing campaign", "digital marketing", "brand awareness",
      "content strategy", "demand generation", "campaign performance",
      "marketing strategy")),
    ("sales", "Sales",
     ("sales quota", "sales pipeline", "new business", "closing deals",
      "prospecting", "revenue growth", "client acquisition")),
    ("human_resources", "Human Resources",
     ("human resources", "talent acquisition", "employee engagement",
      "hr operations", "workforce planning", "people operations")),
    ("design", "Design",
     ("user experience", "user interface", "visual design", "design system",
      "ux/ui", "product design", "graphic design")),
    ("product", "Product Management",
     ("product management", "product strategy", "product roadmap",
      "feature prioritization", "product discovery", "business requirements")),
    ("operations", "Operations",
     ("operations management", "supply chain", "process optimization",
      "project delivery", "logistics", "vendor management",
      "operational efficiency")),
    ("healthcare", "Healthcare",
     ("patient care", "clinical", "healthcare", "medical records",
      "nursing", "patient outcomes")),
    ("customer_support", "Customer Support & Success",
     ("customer support", "customer success", "customer satisfaction",
      "technical support", "client retention", "help desk")),
    # Networking and infrastructure operations. Distinct from devops_cloud:
    # both touch infrastructure, but a network engineer's subject is the
    # network itself - routing, switching, firewalls, connectivity - not
    # delivery pipelines or platform automation. Without this domain a
    # networking resume lands on whichever adjacent domain its incidental
    # cloud vocabulary happened to hit.
    #
    # "network security" is deliberately absent: it already signals
    # cybersecurity, and one keyword cannot belong to two domains.
    ("network_infrastructure", "Network & Infrastructure",
     ("network infrastructure", "network operations", "network engineering",
      "network administration", "routing and switching", "enterprise network",
      "network monitoring", "network troubleshooting", "lan/wan",
      "network connectivity")),
)


# --- Roles -----------------------------------------------------------------
# (key, canonical_title, domain_key, aliases, variations, core_skills)
#
# aliases    : surface forms in a resume that identify this role. The canonical
#              title is matched automatically and need not be repeated.
# variations : same-role search titles. Seniority-prefixed forms are generated
#              at runtime, so do not list "Senior X" / "Junior X" here.
# core_skills: skill keys from skills_data.py used as corroborating evidence.
# fmt: off
ROLE_ENTRIES: tuple[
    tuple[str, str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...
] = (
    # ---------------- Software engineering --------------------------------
    ("software_engineer", "Software Engineer", "software_engineering",
     ("software developer", "sde", "software development engineer",
      "application developer", "programmer", "software design engineer"),
     ("Software Engineer", "Software Developer", "Software Development Engineer",
      "Application Developer", "Applications Engineer"),
     ("python", "java", "sql", "git", "rest api", "microservices")),

    ("frontend_engineer", "Frontend Engineer", "software_engineering",
     ("front end engineer", "front-end engineer", "frontend developer",
      "front end developer", "front-end developer", "ui engineer",
      "react developer", "javascript developer"),
     ("Frontend Engineer", "Front End Developer", "Frontend Developer",
      "UI Engineer", "Frontend Software Engineer", "Web Frontend Engineer"),
     ("javascript", "typescript", "react", "html", "css")),

    ("backend_engineer", "Backend Engineer", "software_engineering",
     ("back end engineer", "back-end engineer", "backend developer",
      "back end developer", "server side engineer", "api engineer"),
     ("Backend Engineer", "Back End Developer", "Backend Developer",
      "Backend Software Engineer", "Server Side Engineer", "API Engineer"),
     ("python", "java", "sql", "rest api", "microservices", "postgresql")),

    ("full_stack_engineer", "Full Stack Engineer", "software_engineering",
     ("fullstack engineer", "full-stack engineer", "full stack developer",
      "fullstack developer", "full-stack developer"),
     ("Full Stack Engineer", "Full Stack Developer", "Fullstack Engineer",
      "Full Stack Software Engineer", "Full Stack Web Developer"),
     ("javascript", "react", "node.js", "sql", "rest api")),

    ("mobile_engineer", "Mobile Engineer", "software_engineering",
     ("mobile developer", "ios developer", "android developer",
      "mobile application developer", "ios engineer", "android engineer"),
     ("Mobile Engineer", "Mobile Developer", "Mobile Application Developer",
      "iOS Developer", "Android Developer", "Mobile Software Engineer"),
     ("swift", "kotlin", "react native", "flutter")),

    ("qa_engineer", "QA Engineer", "software_engineering",
     ("quality assurance engineer", "test engineer", "sdet",
      "automation test engineer", "qa analyst", "software test engineer"),
     ("QA Engineer", "Quality Assurance Engineer", "Test Engineer",
      "Automation Test Engineer", "SDET", "QA Automation Engineer"),
     ("python", "agile", "git")),

    # ---------------- Data -------------------------------------------------
    ("data_analyst", "Data Analyst", "data",
     ("analytics analyst", "reporting analyst", "insights analyst"),
     ("Data Analyst", "Analytics Analyst", "Reporting Analyst",
      "Insights Analyst", "Data Insights Analyst"),
     ("sql", "excel", "tableau", "power bi", "data visualization")),

    ("bi_analyst", "Business Intelligence Analyst", "data",
     ("bi analyst", "bi developer", "business intelligence developer"),
     ("Business Intelligence Analyst", "BI Analyst", "BI Developer",
      "Business Intelligence Developer", "BI Reporting Analyst"),
     ("sql", "power bi", "tableau", "data warehousing", "etl")),

    ("data_scientist", "Data Scientist", "data",
     ("applied scientist", "research scientist data"),
     ("Data Scientist", "Applied Data Scientist",
      "Machine Learning Data Scientist", "Product Data Scientist"),
     ("python", "machine learning", "statistics", "pandas", "scikit-learn")),

    ("data_engineer", "Data Engineer", "data",
     ("etl developer", "data pipeline engineer", "big data engineer"),
     ("Data Engineer", "Big Data Engineer", "ETL Developer",
      "Data Pipeline Engineer", "Analytics Engineer"),
     ("python", "sql", "spark", "airflow", "etl", "data modeling")),

    ("machine_learning_engineer", "Machine Learning Engineer", "data",
     ("ml engineer", "mlops engineer", "ai engineer"),
     ("Machine Learning Engineer", "ML Engineer", "AI Engineer",
      "MLOps Engineer", "Applied Machine Learning Engineer"),
     ("python", "machine learning", "tensorflow", "pytorch", "deep learning")),

    # ---------------- DevOps and cloud -------------------------------------
    ("devops_engineer", "DevOps Engineer", "devops_cloud",
     ("dev ops engineer", "build and release engineer", "automation engineer"),
     ("DevOps Engineer", "DevOps Specialist", "Build and Release Engineer",
      "CI/CD Engineer", "Infrastructure Automation Engineer"),
     ("docker", "kubernetes", "terraform", "ci/cd", "aws", "linux")),

    ("site_reliability_engineer", "Site Reliability Engineer", "devops_cloud",
     ("sre", "reliability engineer", "production engineer"),
     ("Site Reliability Engineer", "SRE", "Reliability Engineer",
      "Production Engineer", "Infrastructure Reliability Engineer"),
     ("kubernetes", "prometheus", "linux", "ci/cd", "grafana")),

    ("cloud_engineer", "Cloud Engineer", "devops_cloud",
     ("cloud infrastructure engineer", "aws engineer", "azure engineer",
      "cloud architect"),
     ("Cloud Engineer", "Cloud Infrastructure Engineer", "Cloud Architect",
      "AWS Cloud Engineer", "Cloud Systems Engineer"),
     ("aws", "azure", "gcp", "terraform", "infrastructure as code")),

    ("platform_engineer", "Platform Engineer", "devops_cloud",
     ("infrastructure engineer", "systems engineer platform"),
     ("Platform Engineer", "Infrastructure Engineer",
      "Developer Platform Engineer", "Internal Platform Engineer"),
     ("kubernetes", "terraform", "docker", "ci/cd")),

    # ---------------- Cybersecurity ----------------------------------------
    ("security_engineer", "Security Engineer", "cybersecurity",
     ("information security engineer", "cyber security engineer",
      "cybersecurity engineer", "application security engineer"),
     ("Security Engineer", "Information Security Engineer",
      "Cybersecurity Engineer", "Application Security Engineer",
      "Infrastructure Security Engineer"),
     ("penetration testing", "threat modeling", "firewall", "incident response")),

    ("security_analyst", "Security Analyst", "cybersecurity",
     ("information security analyst", "soc analyst", "cyber security analyst",
      "cybersecurity analyst"),
     ("Security Analyst", "Information Security Analyst", "SOC Analyst",
      "Cybersecurity Analyst", "Security Operations Analyst"),
     ("siem", "incident response", "vulnerability assessment", "soc")),

    # ---------------- Finance ----------------------------------------------
    # NOTE: Financial Analyst is CLAUDE.md's worked example. Its variations
    # contain only re-phrasings of the same job. Accountant, Auditor, Finance
    # Manager, Financial Advisor, Business Analyst and Data Analyst are
    # deliberately separate roles and must never appear here.
    ("financial_analyst", "Financial Analyst", "finance",
     ("fp&a analyst", "fpa analyst", "financial planning analyst",
      "corporate financial analyst", "finance reporting analyst",
      "financial reporting analyst", "finance analyst",
      "financial planning and analysis analyst", "budget analyst"),
     ("Financial Analyst", "FP&A Analyst", "Financial Planning Analyst",
      "Corporate Financial Analyst", "Finance Reporting Analyst",
      "Financial Reporting Analyst", "Financial Planning and Analysis Analyst"),
     ("financial modeling", "financial analysis", "forecasting", "budgeting",
      "variance analysis", "excel", "fp&a")),

    ("investment_analyst", "Investment Analyst", "finance",
     ("equity research analyst", "portfolio analyst", "research analyst finance"),
     ("Investment Analyst", "Equity Research Analyst", "Portfolio Analyst",
      "Investment Research Analyst"),
     ("valuation", "dcf", "financial modeling", "bloomberg")),

    ("finance_manager", "Finance Manager", "finance",
     ("finance lead", "financial manager", "fp&a manager"),
     ("Finance Manager", "Financial Manager", "FP&A Manager",
      "Finance Team Manager"),
     ("budgeting", "forecasting", "financial reporting", "fp&a")),

    ("financial_advisor", "Financial Advisor", "finance",
     ("wealth advisor", "financial planner", "wealth management advisor"),
     ("Financial Advisor", "Financial Planner", "Wealth Advisor",
      "Personal Financial Advisor"),
     ("valuation", "communication")),

    # ---------------- Accounting -------------------------------------------
    ("accountant", "Accountant", "accounting",
     ("staff accountant", "general accountant", "corporate accountant",
      "senior accountant"),
     ("Accountant", "Staff Accountant", "General Accountant",
      "Corporate Accountant", "General Ledger Accountant"),
     ("general ledger", "reconciliation", "month end close", "gaap", "excel")),

    ("auditor", "Auditor", "accounting",
     ("internal auditor", "external auditor", "audit associate",
      "audit senior"),
     ("Auditor", "Internal Auditor", "External Auditor", "Audit Associate",
      "Audit Analyst"),
     ("auditing", "internal controls", "gaap", "financial reporting")),

    ("tax_accountant", "Tax Accountant", "accounting",
     ("tax associate", "tax analyst", "tax specialist"),
     ("Tax Accountant", "Tax Analyst", "Tax Associate", "Tax Specialist"),
     ("tax preparation", "gaap", "excel")),

    ("accounts_payable_specialist", "Accounts Payable Specialist", "accounting",
     ("ap specialist", "accounts payable clerk", "accounts payable analyst"),
     ("Accounts Payable Specialist", "Accounts Payable Analyst",
      "AP Specialist", "Accounts Payable Clerk"),
     ("accounts payable", "reconciliation", "excel")),

    # ---------------- Marketing --------------------------------------------
    ("digital_marketing_specialist", "Digital Marketing Specialist", "marketing",
     ("digital marketing executive", "digital marketer",
      "digital marketing associate"),
     ("Digital Marketing Specialist", "Digital Marketing Associate",
      "Digital Marketing Executive", "Digital Marketer"),
     ("seo", "google ads", "google analytics", "email marketing")),

    ("content_marketing_manager", "Content Marketing Manager", "marketing",
     ("content manager", "content strategist", "content marketing specialist"),
     ("Content Marketing Manager", "Content Marketing Specialist",
      "Content Strategist", "Content Manager"),
     ("content marketing", "copywriting", "seo")),

    ("seo_specialist", "SEO Specialist", "marketing",
     ("seo analyst", "search engine optimization specialist", "seo executive"),
     ("SEO Specialist", "SEO Analyst", "SEO Executive",
      "Search Engine Optimization Specialist"),
     ("seo", "google analytics", "sem")),

    ("performance_marketing_manager", "Performance Marketing Manager", "marketing",
     ("paid media manager", "growth marketing manager", "ppc manager"),
     ("Performance Marketing Manager", "Paid Media Manager",
      "Growth Marketing Manager", "PPC Manager"),
     ("ppc", "google ads", "sem", "marketing automation")),

    ("marketing_manager", "Marketing Manager", "marketing",
     ("brand manager", "marketing lead"),
     ("Marketing Manager", "Brand Manager", "Marketing Team Manager"),
     ("brand strategy", "marketing automation", "content marketing")),

    # ---------------- Sales -------------------------------------------------
    ("sales_development_representative", "Sales Development Representative", "sales",
     ("sdr", "business development representative", "bdr",
      "inside sales representative"),
     ("Sales Development Representative", "SDR",
      "Business Development Representative", "Inside Sales Representative"),
     ("lead generation", "cold calling", "salesforce", "outreach")),

    ("account_executive", "Account Executive", "sales",
     ("ae", "sales executive", "enterprise account executive"),
     ("Account Executive", "Sales Executive", "Enterprise Account Executive",
      "Mid-Market Account Executive"),
     ("b2b sales", "pipeline management", "negotiation", "salesforce")),

    ("account_manager", "Account Manager", "sales",
     ("client account manager", "key account manager"),
     ("Account Manager", "Key Account Manager", "Client Account Manager",
      "Strategic Account Manager"),
     ("account management", "crm", "negotiation")),

    ("sales_manager", "Sales Manager", "sales",
     ("regional sales manager", "sales team lead"),
     ("Sales Manager", "Regional Sales Manager", "Sales Team Manager"),
     ("pipeline management", "negotiation", "leadership")),

    # ---------------- Human resources ---------------------------------------
    ("hr_generalist", "HR Generalist", "human_resources",
     ("human resources generalist", "hr associate", "hr coordinator",
      "human resources associate"),
     ("HR Generalist", "Human Resources Generalist", "HR Associate",
      "Human Resources Coordinator"),
     ("employee relations", "onboarding", "hris", "hr policy")),

    ("recruiter", "Recruiter", "human_resources",
     ("talent acquisition specialist", "technical recruiter",
      "talent acquisition partner", "recruitment specialist"),
     ("Recruiter", "Technical Recruiter", "Talent Acquisition Specialist",
      "Talent Acquisition Partner", "Recruitment Specialist"),
     ("recruiting", "ats", "onboarding")),

    ("hr_business_partner", "HR Business Partner", "human_resources",
     ("hrbp", "people business partner"),
     ("HR Business Partner", "HRBP", "People Business Partner",
      "Human Resources Business Partner"),
     ("employee relations", "performance management", "hr policy")),

    ("hr_manager", "HR Manager", "human_resources",
     ("human resources manager", "people operations manager"),
     ("HR Manager", "Human Resources Manager", "People Operations Manager"),
     ("employee relations", "compensation", "performance management")),

    # ---------------- Design -------------------------------------------------
    ("ux_designer", "UX Designer", "design",
     ("user experience designer", "ux/ui designer", "ux ui designer"),
     ("UX Designer", "User Experience Designer", "UX/UI Designer",
      "User Experience Design Specialist"),
     ("figma", "user research", "wireframing", "prototyping",
      "usability testing")),

    ("ui_designer", "UI Designer", "design",
     ("user interface designer", "visual designer"),
     ("UI Designer", "User Interface Designer", "Visual Designer",
      "UI Visual Designer"),
     ("figma", "design systems", "typography", "prototyping")),

    ("product_designer", "Product Designer", "design",
     ("digital product designer",),
     ("Product Designer", "Digital Product Designer",
      "Product Experience Designer"),
     ("figma", "prototyping", "design systems", "user research")),

    ("graphic_designer", "Graphic Designer", "design",
     ("visual graphic designer", "brand designer"),
     ("Graphic Designer", "Brand Designer", "Visual Communications Designer"),
     ("photoshop", "illustrator", "typography")),

    # ---------------- Product ------------------------------------------------
    ("product_manager", "Product Manager", "product",
     ("pm product", "technical product manager", "senior product manager",
      "associate product manager"),
     ("Product Manager", "Technical Product Manager",
      "Associate Product Manager", "Digital Product Manager"),
     ("product roadmap", "user stories", "agile", "stakeholder management")),

    ("product_owner", "Product Owner", "product",
     ("po scrum", "agile product owner"),
     ("Product Owner", "Agile Product Owner", "Technical Product Owner"),
     ("user stories", "scrum", "agile", "product roadmap")),

    # Deliberately separate from Data Analyst and Financial Analyst.
    ("business_analyst", "Business Analyst", "product",
     ("ba business", "business systems analyst",
      "business process analyst", "functional analyst"),
     ("Business Analyst", "Business Systems Analyst",
      "Business Process Analyst", "Functional Business Analyst"),
     ("requirements gathering", "stakeholder management", "sql", "agile")),

    # ---------------- Operations ---------------------------------------------
    ("operations_manager", "Operations Manager", "operations",
     ("ops manager", "business operations manager"),
     ("Operations Manager", "Business Operations Manager",
      "Operations Team Manager"),
     ("process improvement", "vendor management", "project management")),

    ("supply_chain_analyst", "Supply Chain Analyst", "operations",
     ("logistics analyst", "supply chain specialist", "demand planner"),
     ("Supply Chain Analyst", "Logistics Analyst", "Supply Chain Specialist",
      "Demand Planning Analyst"),
     ("supply chain", "inventory management", "demand planning", "excel")),

    ("project_manager", "Project Manager", "operations",
     ("project lead", "delivery manager", "program manager"),
     ("Project Manager", "Delivery Manager", "Program Manager",
      "Technical Project Manager"),
     ("project management", "stakeholder management", "agile", "jira")),

    ("business_operations_analyst", "Business Operations Analyst", "operations",
     ("bizops analyst", "operations analyst"),
     ("Business Operations Analyst", "Operations Analyst",
      "BizOps Analyst"),
     ("process improvement", "excel", "sql")),

    # ---------------- Healthcare ----------------------------------------------
    ("registered_nurse", "Registered Nurse", "healthcare",
     ("rn", "staff nurse", "clinical nurse"),
     ("Registered Nurse", "Staff Nurse", "Clinical Nurse", "RN"),
     ("patient care", "medication administration", "emr", "vital signs")),

    ("medical_assistant", "Medical Assistant", "healthcare",
     ("clinical medical assistant", "certified medical assistant"),
     ("Medical Assistant", "Clinical Medical Assistant",
      "Certified Medical Assistant"),
     ("patient care", "vital signs", "phlebotomy", "emr")),

    # ---------------- Customer support ----------------------------------------
    ("customer_support_specialist", "Customer Support Specialist", "customer_support",
     ("customer service representative", "technical support specialist",
      "customer support representative", "help desk specialist",
      "support engineer"),
     ("Customer Support Specialist", "Customer Support Representative",
      "Technical Support Specialist", "Customer Service Representative",
      "Help Desk Specialist"),
     ("customer support", "zendesk", "ticketing", "escalation management")),

    ("customer_success_manager", "Customer Success Manager", "customer_support",
     ("csm", "client success manager"),
     ("Customer Success Manager", "Client Success Manager",
      "Customer Success Specialist"),
     ("customer onboarding", "churn reduction", "account management", "crm")),

    # ---------------- Networking and infrastructure ---------------------------
    # These are separate roles from Customer Support Specialist, IT Support and
    # Help Desk, and separate from DevOps/Cloud Engineer, for the usual reason:
    # adjacent jobs get their own rows so they can never leak into one
    # another's variations.
    #
    # Their absence was a real defect, not an omission of coverage. Titles are
    # only extracted from a resume when they match a known role, so a resume
    # reading "Network Engineer | June 2024 - Present" had *no* title
    # extracted at all - while "Network Support Engineer Intern" three jobs
    # earlier matched the alias "support engineer" and became the most recent
    # title by default. The fix is that the real titles are now visible; the
    # qualified forms below ("network support engineer") also matter, because
    # the matcher prefers the longest known term and so stops reading a
    # networking title as a support one.
    ("network_engineer", "Network Engineer", "network_infrastructure",
     ("network support engineer", "networking engineer", "network systems engineer",
      "systems network engineer", "network infrastructure engineer",
      "network operations engineer", "lan/wan engineer"),
     ("Network Engineer", "Network Systems Engineer",
      "Network Infrastructure Engineer", "Enterprise Network Engineer",
      "Network Operations Engineer"),
     ("tcp/ip", "routing", "switching", "cisco", "vlan", "vpn")),

    ("network_administrator", "Network Administrator", "network_infrastructure",
     ("network admin", "network systems administrator", "lan administrator",
      "network operations administrator"),
     ("Network Administrator", "Network Systems Administrator",
      "LAN Administrator", "Network Operations Administrator"),
     ("tcp/ip", "routing", "switching", "dns", "dhcp")),

    # Domain is cybersecurity rather than network_infrastructure: the subject
    # of the work is the security control, and "network security" already
    # signals that domain. The role stays distinct from Network Engineer so
    # neither searches the other's titles.
    ("network_security_engineer", "Network Security Engineer", "cybersecurity",
     ("firewall engineer", "network security specialist",
      "perimeter security engineer"),
     ("Network Security Engineer", "Firewall Engineer",
      "Network Security Specialist", "Perimeter Security Engineer"),
     ("firewall", "vpn", "network segmentation", "tcp/ip")),

    ("noc_engineer", "NOC Engineer", "network_infrastructure",
     ("noc", "network operations center engineer", "noc analyst",
      "noc technician", "network operations center technician"),
     ("NOC Engineer", "NOC Analyst", "Network Operations Center Engineer",
      "NOC Technician"),
     ("network monitoring", "tcp/ip", "routing", "incident response")),
)
# fmt: on
