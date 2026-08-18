"""Skill, technology and tool registry.

Data only - no logic. Each entry is
``(key, display, kind, aliases, domain_keys)``.

``key`` is the normalised canonical form used for set comparisons. ``aliases``
are additional surface forms found in resumes; the key itself is always
matched and need not be repeated. ``domain_keys`` lists the domains the skill
contributes evidence toward - leave empty for skills that are too general to
signal a domain (e.g. "communication").

Extending this file is the intended way to improve extraction coverage.
"""

from __future__ import annotations

from app.core.constants import SkillKind

SKILL = SkillKind.SKILL
TECH = SkillKind.TECHNOLOGY
TOOL = SkillKind.TOOL

# fmt: off
SKILL_ENTRIES: tuple[tuple[str, str, SkillKind, tuple[str, ...], tuple[str, ...]], ...] = (
    # --- Programming languages --------------------------------------------
    ("python", "Python", TECH, (), ("software_engineering", "data", "devops_cloud")),
    ("java", "Java", TECH, (), ("software_engineering",)),
    ("javascript", "JavaScript", TECH, ("js", "ecmascript"), ("software_engineering",)),
    ("typescript", "TypeScript", TECH, ("ts",), ("software_engineering",)),
    ("c#", "C#", TECH, ("c sharp", "csharp"), ("software_engineering",)),
    ("c++", "C++", TECH, ("cpp",), ("software_engineering",)),
    ("c", "C", TECH, (), ("software_engineering",)),
    ("go", "Go", TECH, ("golang",), ("software_engineering", "devops_cloud")),
    ("rust", "Rust", TECH, (), ("software_engineering",)),
    ("ruby", "Ruby", TECH, (), ("software_engineering",)),
    ("php", "PHP", TECH, (), ("software_engineering",)),
    ("swift", "Swift", TECH, (), ("software_engineering",)),
    ("kotlin", "Kotlin", TECH, (), ("software_engineering",)),
    ("scala", "Scala", TECH, (), ("data",)),
    ("r", "R", TECH, (), ("data",)),
    ("sql", "SQL", TECH, (), ("data", "software_engineering", "finance")),
    ("bash", "Bash", TECH, ("shell scripting", "shell"), ("devops_cloud",)),
    ("powershell", "PowerShell", TECH, (), ("devops_cloud",)),

    # --- Web and app frameworks -------------------------------------------
    ("react", "React", TECH, ("react.js", "reactjs"), ("software_engineering",)),
    ("angular", "Angular", TECH, ("angularjs",), ("software_engineering",)),
    ("vue", "Vue", TECH, ("vue.js", "vuejs"), ("software_engineering",)),
    ("next.js", "Next.js", TECH, ("nextjs",), ("software_engineering",)),
    ("node.js", "Node.js", TECH, ("nodejs", "node"), ("software_engineering",)),
    ("django", "Django", TECH, (), ("software_engineering",)),
    ("flask", "Flask", TECH, (), ("software_engineering",)),
    ("fastapi", "FastAPI", TECH, (), ("software_engineering",)),
    ("spring boot", "Spring Boot", TECH, ("spring",), ("software_engineering",)),
    (".net", ".NET", TECH, ("dotnet", "asp.net"), ("software_engineering",)),
    ("express", "Express", TECH, ("express.js",), ("software_engineering",)),
    ("rails", "Ruby on Rails", TECH, ("ruby on rails",), ("software_engineering",)),
    ("react native", "React Native", TECH, (), ("software_engineering",)),
    ("flutter", "Flutter", TECH, (), ("software_engineering",)),
    ("html", "HTML", TECH, ("html5",), ("software_engineering", "design")),
    ("css", "CSS", TECH, ("css3", "scss", "sass"), ("software_engineering", "design")),
    ("tailwind", "Tailwind CSS", TECH, ("tailwind css",), ("software_engineering",)),
    ("graphql", "GraphQL", TECH, (), ("software_engineering",)),
    ("rest api", "REST API", TECH, ("restful api", "rest apis", "restful"), ("software_engineering",)),
    ("grpc", "gRPC", TECH, (), ("software_engineering",)),
    ("microservices", "Microservices", SKILL, (), ("software_engineering",)),

    # --- Databases ---------------------------------------------------------
    ("postgresql", "PostgreSQL", TECH, ("postgres",), ("software_engineering", "data")),
    ("mysql", "MySQL", TECH, (), ("software_engineering", "data")),
    ("mongodb", "MongoDB", TECH, ("mongo",), ("software_engineering", "data")),
    ("redis", "Redis", TECH, (), ("software_engineering", "devops_cloud")),
    ("elasticsearch", "Elasticsearch", TECH, (), ("software_engineering", "data")),
    ("sql server", "SQL Server", TECH, ("mssql", "microsoft sql server"), ("data",)),
    ("oracle", "Oracle Database", TECH, ("oracle db",), ("data",)),
    ("snowflake", "Snowflake", TECH, (), ("data",)),
    ("bigquery", "BigQuery", TECH, ("google bigquery",), ("data",)),
    ("redshift", "Amazon Redshift", TECH, ("aws redshift",), ("data",)),
    ("dynamodb", "DynamoDB", TECH, (), ("software_engineering", "devops_cloud")),
    ("cassandra", "Cassandra", TECH, (), ("data",)),

    # --- Cloud, infra and DevOps ------------------------------------------
    ("aws", "AWS", TECH, ("amazon web services",), ("devops_cloud", "software_engineering")),
    ("azure", "Azure", TECH, ("microsoft azure",), ("devops_cloud",)),
    ("gcp", "Google Cloud Platform", TECH, ("google cloud",), ("devops_cloud",)),
    ("docker", "Docker", TOOL, (), ("devops_cloud", "software_engineering")),
    ("kubernetes", "Kubernetes", TOOL, ("k8s",), ("devops_cloud",)),
    ("terraform", "Terraform", TOOL, (), ("devops_cloud",)),
    ("ansible", "Ansible", TOOL, (), ("devops_cloud",)),
    ("jenkins", "Jenkins", TOOL, (), ("devops_cloud",)),
    ("github actions", "GitHub Actions", TOOL, (), ("devops_cloud",)),
    ("gitlab ci", "GitLab CI", TOOL, ("gitlab ci/cd",), ("devops_cloud",)),
    ("ci/cd", "CI/CD", SKILL, ("continuous integration", "continuous delivery", "continuous deployment"), ("devops_cloud",)),
    ("linux", "Linux", TECH, ("unix",), ("devops_cloud",)),
    ("prometheus", "Prometheus", TOOL, (), ("devops_cloud",)),
    ("grafana", "Grafana", TOOL, (), ("devops_cloud",)),
    ("datadog", "Datadog", TOOL, (), ("devops_cloud",)),
    ("nginx", "Nginx", TOOL, (), ("devops_cloud",)),
    ("kafka", "Apache Kafka", TECH, ("kafka",), ("data", "software_engineering")),
    ("rabbitmq", "RabbitMQ", TECH, (), ("software_engineering",)),
    ("git", "Git", TOOL, (), ("software_engineering", "devops_cloud")),
    ("infrastructure as code", "Infrastructure as Code", SKILL, ("iac",), ("devops_cloud",)),

    # --- Data and analytics ------------------------------------------------
    ("pandas", "pandas", TECH, (), ("data",)),
    ("numpy", "NumPy", TECH, (), ("data",)),
    ("scikit-learn", "scikit-learn", TECH, ("sklearn", "scikit learn"), ("data",)),
    ("tensorflow", "TensorFlow", TECH, (), ("data",)),
    ("pytorch", "PyTorch", TECH, (), ("data",)),
    ("spark", "Apache Spark", TECH, ("pyspark", "apache spark"), ("data",)),
    ("hadoop", "Hadoop", TECH, (), ("data",)),
    ("airflow", "Apache Airflow", TOOL, ("apache airflow",), ("data",)),
    ("dbt", "dbt", TOOL, (), ("data",)),
    ("tableau", "Tableau", TOOL, (), ("data", "finance")),
    ("power bi", "Power BI", TOOL, ("powerbi",), ("data", "finance")),
    ("looker", "Looker", TOOL, (), ("data",)),
    ("machine learning", "Machine Learning", SKILL, ("ml",), ("data",)),
    ("deep learning", "Deep Learning", SKILL, (), ("data",)),
    ("nlp", "Natural Language Processing", SKILL, ("natural language processing",), ("data",)),
    ("data modeling", "Data Modeling", SKILL, ("data modelling",), ("data",)),
    ("etl", "ETL", SKILL, ("elt", "etl pipelines"), ("data",)),
    ("data warehousing", "Data Warehousing", SKILL, ("data warehouse",), ("data",)),
    ("data visualization", "Data Visualization", SKILL, ("data visualisation",), ("data",)),
    ("statistics", "Statistics", SKILL, ("statistical analysis",), ("data",)),
    ("ab testing", "A/B Testing", SKILL, ("a/b testing", "split testing"), ("data", "product", "marketing")),
    ("predictive modeling", "Predictive Modeling", SKILL, ("predictive modelling",), ("data",)),

    # --- Security ----------------------------------------------------------
    ("penetration testing", "Penetration Testing", SKILL, ("pen testing", "pentesting"), ("cybersecurity",)),
    ("vulnerability assessment", "Vulnerability Assessment", SKILL, ("vulnerability management",), ("cybersecurity",)),
    ("siem", "SIEM", TOOL, ("splunk",), ("cybersecurity",)),
    ("incident response", "Incident Response", SKILL, (), ("cybersecurity",)),
    ("threat modeling", "Threat Modeling", SKILL, ("threat modelling",), ("cybersecurity",)),
    ("iso 27001", "ISO 27001", SKILL, (), ("cybersecurity",)),
    ("nist", "NIST Framework", SKILL, ("nist framework",), ("cybersecurity",)),
    ("firewall", "Firewalls", TECH, ("firewalls",), ("cybersecurity",)),
    ("soc", "Security Operations Center", SKILL, ("security operations center",), ("cybersecurity",)),

    # --- Networking and infrastructure ------------------------------------
    # Corroborating evidence for the networking roles. These are what a
    # network engineer's resume is actually made of, and without them those
    # roles would score on their title alone.
    ("tcp/ip", "TCP/IP", TECH, ("tcp ip", "ipv4", "ipv6"), ("network_infrastructure",)),
    ("routing", "Routing", SKILL, ("ip routing", "inter-vlan routing"), ("network_infrastructure",)),
    ("switching", "Switching", SKILL, ("layer 2 switching",), ("network_infrastructure",)),
    ("cisco", "Cisco", TECH, ("cisco ios", "cisco catalyst", "cisco isr"), ("network_infrastructure",)),
    ("vlan", "VLAN", TECH, ("vlans",), ("network_infrastructure",)),
    ("ospf", "OSPF", TECH, (), ("network_infrastructure",)),
    ("bgp", "BGP", TECH, (), ("network_infrastructure",)),
    ("eigrp", "EIGRP", TECH, (), ("network_infrastructure",)),
    ("vpn", "VPN", TECH, ("ipsec vpn", "site-to-site vpn"), ("network_infrastructure",)),
    ("dns", "DNS", TECH, (), ("network_infrastructure",)),
    ("dhcp", "DHCP", TECH, (), ("network_infrastructure",)),
    ("nat", "NAT", TECH, ("network address translation",), ("network_infrastructure",)),
    ("subnetting", "Subnetting", SKILL, ("ip addressing",), ("network_infrastructure",)),
    ("wireshark", "Wireshark", TOOL, ("packet capture", "packet analysis"), ("network_infrastructure",)),
    ("solarwinds", "SolarWinds", TOOL, (), ("network_infrastructure",)),
    ("network monitoring", "Network Monitoring", SKILL, ("network performance monitoring",), ("network_infrastructure",)),
    ("load balancing", "Load Balancing", SKILL, ("load balancer",), ("network_infrastructure", "devops_cloud")),
    ("wireless networking", "Wireless Networking", SKILL, ("wifi", "wlan"), ("network_infrastructure",)),
    ("network segmentation", "Network Segmentation", SKILL, (), ("network_infrastructure", "cybersecurity")),

    # --- Finance and accounting -------------------------------------------
    ("financial modeling", "Financial Modeling", SKILL, ("financial modelling",), ("finance",)),
    ("financial analysis", "Financial Analysis", SKILL, (), ("finance",)),
    ("forecasting", "Forecasting", SKILL, ("financial forecasting",), ("finance",)),
    ("budgeting", "Budgeting", SKILL, ("budget planning",), ("finance",)),
    ("variance analysis", "Variance Analysis", SKILL, (), ("finance",)),
    ("fp&a", "FP&A", SKILL, ("financial planning and analysis", "financial planning & analysis"), ("finance",)),
    ("valuation", "Valuation", SKILL, (), ("finance",)),
    ("dcf", "DCF Analysis", SKILL, ("discounted cash flow",), ("finance",)),
    ("financial reporting", "Financial Reporting", SKILL, (), ("finance", "accounting")),
    ("general ledger", "General Ledger", SKILL, ("gl",), ("accounting",)),
    ("accounts payable", "Accounts Payable", SKILL, ("ap",), ("accounting",)),
    ("accounts receivable", "Accounts Receivable", SKILL, ("ar",), ("accounting",)),
    ("reconciliation", "Account Reconciliation", SKILL, ("account reconciliation", "bank reconciliation"), ("accounting",)),
    ("month end close", "Month-End Close", SKILL, ("month-end close", "monthly close"), ("accounting",)),
    ("gaap", "GAAP", SKILL, ("us gaap",), ("accounting",)),
    ("ifrs", "IFRS", SKILL, (), ("accounting",)),
    ("tax preparation", "Tax Preparation", SKILL, ("tax filing",), ("accounting",)),
    ("internal controls", "Internal Controls", SKILL, ("sox", "sarbanes-oxley"), ("accounting",)),
    ("auditing", "Auditing", SKILL, ("audit",), ("accounting",)),
    ("excel", "Microsoft Excel", TOOL, ("microsoft excel", "ms excel", "advanced excel"), ("finance", "accounting", "data")),
    ("quickbooks", "QuickBooks", TOOL, (), ("accounting",)),
    ("sap", "SAP", TOOL, (), ("finance", "accounting", "operations")),
    ("netsuite", "NetSuite", TOOL, (), ("accounting",)),
    ("hyperion", "Oracle Hyperion", TOOL, (), ("finance",)),
    ("bloomberg", "Bloomberg Terminal", TOOL, ("bloomberg terminal",), ("finance",)),

    # --- Marketing ---------------------------------------------------------
    ("seo", "SEO", SKILL, ("search engine optimization", "search engine optimisation"), ("marketing",)),
    ("sem", "SEM", SKILL, ("search engine marketing",), ("marketing",)),
    ("google ads", "Google Ads", TOOL, ("adwords", "google adwords"), ("marketing",)),
    ("google analytics", "Google Analytics", TOOL, ("ga4",), ("marketing",)),
    ("content marketing", "Content Marketing", SKILL, (), ("marketing",)),
    ("email marketing", "Email Marketing", SKILL, (), ("marketing",)),
    ("social media marketing", "Social Media Marketing", SKILL, ("social media",), ("marketing",)),
    ("hubspot", "HubSpot", TOOL, (), ("marketing", "sales")),
    ("marketo", "Marketo", TOOL, (), ("marketing",)),
    ("mailchimp", "Mailchimp", TOOL, (), ("marketing",)),
    ("copywriting", "Copywriting", SKILL, (), ("marketing",)),
    ("brand strategy", "Brand Strategy", SKILL, ("branding",), ("marketing",)),
    ("ppc", "PPC", SKILL, ("pay per click", "paid search"), ("marketing",)),
    ("marketing automation", "Marketing Automation", SKILL, (), ("marketing",)),

    # --- Sales -------------------------------------------------------------
    ("salesforce", "Salesforce", TOOL, ("sfdc",), ("sales", "marketing")),
    ("lead generation", "Lead Generation", SKILL, ("lead gen",), ("sales",)),
    ("cold calling", "Cold Calling", SKILL, ("cold outreach",), ("sales",)),
    ("pipeline management", "Pipeline Management", SKILL, (), ("sales",)),
    ("crm", "CRM", TOOL, (), ("sales", "marketing")),
    ("negotiation", "Negotiation", SKILL, (), ("sales",)),
    ("account management", "Account Management", SKILL, (), ("sales",)),
    ("b2b sales", "B2B Sales", SKILL, ("b2b",), ("sales",)),
    ("quota attainment", "Quota Attainment", SKILL, (), ("sales",)),
    ("outreach", "Outreach", TOOL, ("outreach.io", "salesloft"), ("sales",)),

    # --- Human resources ---------------------------------------------------
    ("recruiting", "Recruiting", SKILL, ("recruitment", "talent acquisition"), ("human_resources",)),
    ("onboarding", "Onboarding", SKILL, (), ("human_resources",)),
    ("employee relations", "Employee Relations", SKILL, (), ("human_resources",)),
    ("performance management", "Performance Management", SKILL, (), ("human_resources",)),
    ("hris", "HRIS", TOOL, ("workday", "bamboohr"), ("human_resources",)),
    ("compensation", "Compensation & Benefits", SKILL, ("compensation and benefits", "benefits administration"), ("human_resources",)),
    ("ats", "Applicant Tracking System", TOOL, ("applicant tracking system", "greenhouse ats", "lever"), ("human_resources",)),
    ("hr policy", "HR Policy", SKILL, ("hr policies",), ("human_resources",)),
    ("employment law", "Employment Law", SKILL, ("labor law",), ("human_resources",)),

    # --- Design ------------------------------------------------------------
    ("figma", "Figma", TOOL, (), ("design",)),
    ("sketch", "Sketch", TOOL, (), ("design",)),
    ("adobe xd", "Adobe XD", TOOL, (), ("design",)),
    ("photoshop", "Adobe Photoshop", TOOL, ("adobe photoshop",), ("design",)),
    ("illustrator", "Adobe Illustrator", TOOL, ("adobe illustrator",), ("design",)),
    ("wireframing", "Wireframing", SKILL, ("wireframes",), ("design",)),
    ("prototyping", "Prototyping", SKILL, (), ("design",)),
    ("user research", "User Research", SKILL, ("ux research",), ("design",)),
    ("usability testing", "Usability Testing", SKILL, (), ("design",)),
    ("design systems", "Design Systems", SKILL, ("design system",), ("design",)),
    ("interaction design", "Interaction Design", SKILL, (), ("design",)),
    ("typography", "Typography", SKILL, (), ("design",)),

    # --- Product and delivery ---------------------------------------------
    ("product roadmap", "Product Roadmap", SKILL, ("roadmapping", "product roadmapping"), ("product",)),
    ("user stories", "User Stories", SKILL, ("story writing",), ("product",)),
    ("agile", "Agile", SKILL, ("agile methodology",), ("product", "software_engineering")),
    ("scrum", "Scrum", SKILL, (), ("product", "software_engineering")),
    ("kanban", "Kanban", SKILL, (), ("product", "operations")),
    ("jira", "Jira", TOOL, (), ("product", "software_engineering")),
    ("confluence", "Confluence", TOOL, (), ("product",)),
    ("requirements gathering", "Requirements Gathering", SKILL, ("requirements analysis",), ("product",)),
    ("stakeholder management", "Stakeholder Management", SKILL, (), ("product", "operations")),
    ("product analytics", "Product Analytics", TOOL, ("amplitude", "mixpanel"), ("product",)),
    ("go to market", "Go-to-Market", SKILL, ("go-to-market", "gtm"), ("product", "marketing")),

    # --- Operations and project delivery ----------------------------------
    ("project management", "Project Management", SKILL, (), ("operations",)),
    ("supply chain", "Supply Chain Management", SKILL, ("supply chain management",), ("operations",)),
    ("inventory management", "Inventory Management", SKILL, (), ("operations",)),
    ("logistics", "Logistics", SKILL, (), ("operations",)),
    ("procurement", "Procurement", SKILL, ("sourcing",), ("operations",)),
    ("process improvement", "Process Improvement", SKILL, ("continuous improvement",), ("operations",)),
    ("lean", "Lean", SKILL, ("lean manufacturing",), ("operations",)),
    ("six sigma", "Six Sigma", SKILL, (), ("operations",)),
    ("vendor management", "Vendor Management", SKILL, (), ("operations",)),
    ("erp", "ERP", TOOL, (), ("operations",)),
    ("demand planning", "Demand Planning", SKILL, (), ("operations",)),

    # --- Healthcare --------------------------------------------------------
    ("patient care", "Patient Care", SKILL, (), ("healthcare",)),
    ("emr", "EMR/EHR", TOOL, ("ehr", "epic systems", "electronic medical records"), ("healthcare",)),
    ("clinical documentation", "Clinical Documentation", SKILL, (), ("healthcare",)),
    ("medication administration", "Medication Administration", SKILL, (), ("healthcare",)),
    ("vital signs", "Vital Signs Monitoring", SKILL, (), ("healthcare",)),
    ("hipaa", "HIPAA", SKILL, (), ("healthcare",)),
    ("phlebotomy", "Phlebotomy", SKILL, (), ("healthcare",)),
    ("triage", "Triage", SKILL, (), ("healthcare",)),

    # --- Customer support --------------------------------------------------
    ("customer support", "Customer Support", SKILL, ("customer service",), ("customer_support",)),
    ("zendesk", "Zendesk", TOOL, (), ("customer_support",)),
    ("ticketing", "Ticketing Systems", TOOL, ("ticketing systems", "freshdesk"), ("customer_support",)),
    ("customer onboarding", "Customer Onboarding", SKILL, (), ("customer_support",)),
    ("churn reduction", "Churn Reduction", SKILL, ("retention",), ("customer_support",)),
    ("escalation management", "Escalation Management", SKILL, (), ("customer_support",)),
    ("sla management", "SLA Management", SKILL, (), ("customer_support",)),

    # --- General professional skills (no domain signal) -------------------
    ("communication", "Communication", SKILL, ("verbal communication", "written communication"), ()),
    ("leadership", "Leadership", SKILL, (), ()),
    ("problem solving", "Problem Solving", SKILL, (), ()),
    ("teamwork", "Teamwork", SKILL, ("collaboration",), ()),
    ("time management", "Time Management", SKILL, (), ()),
    ("presentation", "Presentation Skills", SKILL, ("presentation skills",), ()),
    ("microsoft office", "Microsoft Office", TOOL, ("ms office",), ()),
    ("powerpoint", "PowerPoint", TOOL, ("microsoft powerpoint",), ()),
    ("word", "Microsoft Word", TOOL, ("microsoft word",), ()),
)
# fmt: on
