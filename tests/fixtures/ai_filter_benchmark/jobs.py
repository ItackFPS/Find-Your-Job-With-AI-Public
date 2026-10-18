"""50 synthetic job postings for the AI-filter benchmark (see docs/ or the
benchmark runner script for context).

STRICT NO-LEAKAGE RULE: this module contains ONLY the raw posting data that a
real collector would produce (title, company, location, description, ...).
It must NEVER contain ground-truth labels, category names, or any hint about
the expected verdict. Ground truth lives exclusively in
``ground_truth.py`` and is joined to these jobs only at evaluation time,
after the production filter/gate/LLM have already produced their verdicts.

Each entry is a plain dict of kwargs accepted by ``src.models.job.Job``.
IDs are stable across reruns (``bm01``..``bm50``) so the ground-truth file
can key off them.
"""

from __future__ import annotations

JOBS: list[dict] = [

    # ================================================================
    # 1-10: Perfect matches
    # ================================================================
    {
        "id": "bm01",
        "title": "Développeur Python Junior H/F",
        "company": "NovaTech Solutions",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "NovaTech Solutions recherche un(e) Développeur Python Junior pour "
            "rejoindre notre équipe backend basée à Paris (2 jours de télétravail/semaine). "
            "Vous participerez au développement et à la maintenance de nos APIs internes, "
            "à l'écriture de scripts d'automatisation et à la correction de bugs sur nos "
            "services Python/Django. Vous travaillerez en méthode agile avec l'équipe produit.\n\n"
            "Missions : développement de nouvelles fonctionnalités backend, écriture de tests "
            "unitaires, participation aux code reviews, utilisation de Git pour le versioning, "
            "administration basique de serveurs Linux.\n\n"
            "Profil recherché : Bac+2 en informatique (BTS SIO, DUT Informatique ou équivalent), "
            "0 à 2 ans d'expérience (stage/alternance accepté). Compétences : Python, MySQL, Git, "
            "Linux, Bash. La connaissance de Docker est un plus. Anglais technique apprécié."
        ),
        "skills": ["Python", "MySQL", "Git", "Linux"],
    },
    {
        "id": "bm02",
        "title": "Développeur Web Junior – JavaScript / HTML / CSS",
        "company": "Lyon Digital Services",
        "location": "Lyon, France (remote possible)",
        "countries": ["France"],
        "remote": True,
        "work_mode": "remote",
        "employment_type": "CDI",
        "description": (
            "Agence web basée à Lyon, nous concevons des sites vitrines et e-commerce pour "
            "des PME françaises. Nous recherchons un(e) Développeur Web Junior en full remote "
            "(déplacements ponctuels à Lyon).\n\n"
            "Missions : intégration HTML/CSS de maquettes, développement de fonctionnalités "
            "JavaScript côté client, corrections de bugs sur des sites existants, utilisation "
            "de Git pour la gestion de version, collaboration avec les designers.\n\n"
            "Profil recherché : Bac+2 minimum en développement web, débutant accepté "
            "(stage de fin d'études validé). Compétences attendues : HTML, CSS, JavaScript, Git. "
            "La connaissance de Node.js serait un plus. Poste ouvert aux jeunes diplômés."
        ),
        "skills": ["HTML", "CSS", "JavaScript", "Git"],
    },
    {
        "id": "bm03",
        "title": "Alternance Testeur QA Automatisation – Python / Selenium",
        "company": "QualiSoft France",
        "location": "Nantes, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "Alternance",
        "description": (
            "QualiSoft France, société de conseil spécialisée en qualité logicielle, recherche "
            "un(e) alternant(e) pour renforcer son équipe de tests automatisés à Nantes.\n\n"
            "Missions : écriture de scripts de tests automatisés en Python avec Selenium, "
            "exécution de campagnes de tests d'automatisation sur nos applications clientes, "
            "rédaction de rapports de bugs, utilisation de Git pour versionner les scripts de "
            "test, participation aux réunions d'équipe agile.\n\n"
            "Profil recherché : préparation d'un Bac+2 ou Bac+3 en informatique en alternance. "
            "Bases en Python indispensables. La connaissance de Git est appréciée. "
            "Une première expérience (stage) en tests logiciels est un plus mais non requise."
        ),
        "skills": ["Python", "Git"],
    },
    {
        "id": "bm04",
        "title": "Développeur Automatisation Junior – n8n / Python",
        "company": "FlowWorks Automation",
        "location": "Toulouse, France",
        "countries": ["France"],
        "remote": True,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "FlowWorks Automation aide les PME à automatiser leurs processus métiers grâce à "
            "des workflows no-code/low-code et des scripts sur mesure. Nous recherchons un(e) "
            "Développeur Automatisation Junior pour notre bureau de Toulouse (télétravail partiel).\n\n"
            "Missions : conception de workflows d'automatisation avec n8n, développement de "
            "scripts Python pour des intégrations personnalisées, connexion d'APIs tierces, "
            "maintenance de scripts Bash pour des tâches planifiées, utilisation de Git.\n\n"
            "Profil recherché : Bac+2 en informatique, 0 à 2 ans d'expérience. Compétences : "
            "Python, n8n (ou tout outil d'automatisation similaire), Bash, Git. La connaissance "
            "de Docker est un plus apprécié mais pas obligatoire."
        ),
        "skills": ["Python", "n8n", "Bash", "Git"],
    },
    {
        "id": "bm05",
        "title": "Analyste Data Junior – Python / MySQL",
        "company": "Meridian Analytics",
        "location": "Bordeaux, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "Meridian Analytics accompagne des entreprises du secteur retail dans l'exploitation "
            "de leurs données commerciales. Nous recrutons un(e) Analyste Data Junior basé(e) à "
            "Bordeaux.\n\n"
            "Missions : extraction et nettoyage de données depuis des bases MySQL, écriture de "
            "scripts Python pour automatiser des rapports récurrents, construction de tableaux "
            "de bord simples, collaboration avec les équipes commerciales pour définir les "
            "indicateurs suivis.\n\n"
            "Profil recherché : Bac+2 (BTS/DUT) en informatique, statistiques ou gestion. "
            "Débutant accepté. Compétences requises : Python, MySQL. La connaissance de Git est "
            "un plus. Bon niveau d'anglais écrit apprécié pour la lecture de documentation technique."
        ),
        "skills": ["Python", "MySQL"],
    },
    {
        "id": "bm06",
        "title": "Technicien Support Informatique Junior",
        "company": "Assist'IT Services",
        "location": "Marseille, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "Assist'IT Services, prestataire de support informatique pour PME, recherche un(e) "
            "Technicien Support Informatique Junior pour son agence de Marseille.\n\n"
            "Missions : prise en charge du support utilisateur de niveau 1 et 2 (téléphone, "
            "ticketing), diagnostic et résolution d'incidents matériels et logiciels, "
            "administration basique de postes sous Linux et Windows, rédaction de procédures "
            "techniques, installation et configuration de postes de travail.\n\n"
            "Profil recherché : Bac+2 en informatique (BTS SIO option SISR ou SLAM). Débutant "
            "accepté, une première expérience en stage est un plus. Compétences : notions Linux, "
            "Bash, bases de scripting appréciées. Sens du service client indispensable."
        ),
        "skills": ["Linux", "Bash"],
    },
    {
        "id": "bm07",
        "title": "Développeur Backend Node.js Junior",
        "company": "BrightPath Software",
        "location": "Remote (France / Europe)",
        "countries": ["France"],
        "remote": True,
        "work_mode": "remote",
        "employment_type": "CDI",
        "description": (
            "BrightPath Software est une scale-up édition d'un SaaS B2B utilisé par des équipes "
            "RH en Europe. Nous recherchons un(e) Développeur Backend Node.js Junior en full "
            "remote depuis la France ou l'Europe.\n\n"
            "Missions : développement d'endpoints API REST en Node.js, requêtes et modélisation "
            "de données MySQL, écriture de tests, participation aux revues de code, utilisation "
            "quotidienne de Git et de GitHub.\n\n"
            "Profil recherché : Bac+2 minimum en développement informatique, 1 à 2 ans "
            "d'expérience souhaités (alternance comptabilisée). Compétences : JavaScript, "
            "Node.js, MySQL, Git. Connaissance de Docker appréciée. Anglais professionnel requis "
            "pour échanger avec l'équipe produit basée aux Pays-Bas."
        ),
        "skills": ["JavaScript", "Node.js", "MySQL", "Git"],
    },
    {
        "id": "bm08",
        "title": "Stage Cybersécurité – Sensibilisation & Audit Basique",
        "company": "SecureNet France",
        "location": "Rennes, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "Stage",
        "description": (
            "SecureNet France, cabinet de conseil en cybersécurité pour collectivités et PME, "
            "propose un stage de 6 mois au sein de son équipe sécurité basée à Rennes.\n\n"
            "Missions : participation à des audits de sécurité basiques (revue de configuration, "
            "vérification de bonnes pratiques), rédaction de supports de sensibilisation pour "
            "les utilisateurs finaux, veille sur les vulnérabilités courantes, découverte des "
            "outils d'analyse réseau sous Linux.\n\n"
            "Profil recherché : étudiant(e) préparant un Bac+2 en cybersécurité, réseaux ou "
            "informatique. Pas d'expérience professionnelle nécessaire pour candidater. Notions "
            "de Linux et de réseaux appréciées. Curiosité et rigueur indispensables."
        ),
        "skills": ["Linux"],
    },
    {
        "id": "bm09",
        "title": "Technicien Réseaux Junior H/F",
        "company": "Atlas Réseaux & Systèmes",
        "location": "Lille, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "Atlas Réseaux & Systèmes intervient sur l'infrastructure réseau de collectivités "
            "locales dans les Hauts-de-France. Nous recrutons un(e) Technicien Réseaux Junior "
            "pour notre agence de Lille.\n\n"
            "Missions : installation et configuration d'équipements réseau (switchs, routeurs), "
            "supervision du bon fonctionnement du réseau, diagnostic de pannes de connectivité, "
            "administration de serveurs Linux, rédaction de scripts Bash pour automatiser des "
            "tâches de supervision.\n\n"
            "Profil recherché : Bac+2 en réseaux et télécommunications (BTS SIO option SISR). "
            "Débutant accepté (stage validé apprécié). Compétences : notions réseau, Linux, Bash. "
            "Permis B requis pour les déplacements sur site client."
        ),
        "skills": ["Linux", "Bash"],
    },
    {
        "id": "bm10",
        "title": "Développeur PHP Junior – Symfony",
        "company": "WebForge Studio",
        "location": "Strasbourg, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "WebForge Studio développe des applications web métier pour des clients dans le "
            "secteur de la logistique. Nous recherchons un(e) Développeur PHP Junior pour notre "
            "équipe de Strasbourg (télétravail 1 jour/semaine).\n\n"
            "Missions : développement de fonctionnalités sur nos applications Symfony, "
            "correction de bugs, écriture de requêtes MySQL, intégration de pages HTML/CSS, "
            "utilisation de Git pour le versioning du code, participation aux points d'équipe "
            "hebdomadaires.\n\n"
            "Profil recherché : Bac+2 en développement informatique, 0 à 2 ans d'expérience. "
            "Compétences : PHP, MySQL, HTML, CSS, Git. La connaissance du framework Symfony est "
            "un plus mais pas indispensable, une formation interne est prévue."
        ),
        "skills": ["PHP", "MySQL", "HTML", "CSS", "Git"],
    },

    # ================================================================
    # 11-20: Partial / borderline matches
    # ================================================================
    {
        "id": "bm11",
        "title": "Support Technique Informatique avec Notions de Scripting",
        "company": "HelpLine Services",
        "location": "Nice, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "HelpLine Services recrute un(e) Technicien Support Informatique pour son centre "
            "d'assistance basé à Nice, au service d'une clientèle de PME régionales.\n\n"
            "Missions : traitement des tickets de support niveau 1/2, dépannage à distance des "
            "postes utilisateurs, rédaction de petits scripts Bash pour automatiser des tâches "
            "de maintenance récurrentes, suivi des incidents dans notre outil de ticketing, "
            "mise à jour de la documentation technique.\n\n"
            "Profil recherché : Bac+2 en informatique (BTS SIO). 1 an d'expérience apprécié mais "
            "débutants motivés acceptés. Compétences : bases Linux, notions de scripting Bash. "
            "Ce poste est avant tout orienté support utilisateur, avec une part de scripting "
            "limitée mais réelle."
        ),
        "skills": ["Linux", "Bash"],
    },
    {
        "id": "bm12",
        "title": "Chargé(e) d'Études BI / Reporting – Python & MySQL",
        "company": "Insight Retail Group",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "Insight Retail Group, enseigne de distribution spécialisée, recherche un(e) "
            "Chargé(e) d'Études BI / Reporting pour son siège parisien.\n\n"
            "Missions : production de reportings commerciaux mensuels pour la direction, "
            "construction de tableaux croisés et de synthèses à partir de requêtes MySQL, "
            "automatisation ponctuelle de tâches répétitives avec des scripts Python simples, "
            "présentation des résultats aux équipes métier. Le poste est majoritairement orienté "
            "analyse et communication de résultats business, avec une utilisation limitée et "
            "occasionnelle de Python pour gagner du temps sur des tâches manuelles.\n\n"
            "Profil recherché : Bac+2/Bac+3 en gestion, statistiques ou informatique décisionnelle. "
            "Compétences : MySQL indispensable, notions de Python appréciées mais non essentielles "
            "au poste. Excellent relationnel requis."
        ),
        "skills": ["MySQL", "Python"],
    },
    {
        "id": "bm13",
        "title": "Testeur QA – Bases en Automatisation Python",
        "company": "CheckPoint QA Consulting",
        "location": "Lyon, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "CheckPoint QA Consulting recherche un(e) Testeur QA pour rejoindre une équipe de "
            "tests principalement manuels, avec une composante automatisation naissante, à Lyon.\n\n"
            "Missions : exécution de campagnes de tests fonctionnels manuels sur des applications "
            "web métier, rédaction de cahiers de tests, remontée d'anomalies dans l'outil de "
            "suivi, participation à l'écriture de premiers scripts d'automatisation en Python "
            "pour les parcours les plus critiques.\n\n"
            "Profil recherché : Bac+2 en informatique. Débutant accepté. Compétences : bases en "
            "Python appréciées pour évoluer vers l'automatisation, la majorité du poste reste "
            "des tests manuels dans un premier temps. Rigueur et sens du détail indispensables."
        ),
        "skills": ["Python"],
    },
    {
        "id": "bm14",
        "title": "Alternant DevOps – Linux / Docker / Bash",
        "company": "CloudBridge Infra",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": True,
        "work_mode": "hybrid",
        "employment_type": "Alternance",
        "description": (
            "CloudBridge Infra gère l'infrastructure cloud de plusieurs startups françaises. "
            "Nous recherchons un(e) alternant(e) pour intégrer notre équipe infrastructure basée "
            "à Paris (télétravail partiel possible).\n\n"
            "Missions : maintenance de conteneurs Docker en environnement de test, écriture de "
            "scripts Bash pour automatiser des tâches de déploiement, administration basique de "
            "serveurs Linux, découverte des pratiques CI/CD, utilisation de Git au quotidien.\n\n"
            "Profil recherché : préparation d'un Bac+2 ou Bac+3 en informatique/réseaux en "
            "alternance. Compétences : notions de Linux, Docker et Bash. Une formation complète "
            "aux outils DevOps avancés sera assurée en interne — ce poste est un point d'entrée "
            "pour un profil junior motivé plutôt qu'un poste DevOps confirmé."
        ),
        "skills": ["Linux", "Docker", "Bash", "Git"],
    },
    {
        "id": "bm15",
        "title": "Développeur Web Junior – Stack Ruby on Rails",
        "company": "RailWorks Digital",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": True,
        "work_mode": "remote",
        "employment_type": "CDI",
        "description": (
            "RailWorks Digital édite une plateforme SaaS de gestion de projets construite "
            "entièrement sur Ruby on Rails. Nous recherchons un(e) Développeur Web Junior pour "
            "renforcer notre équipe produit, en full remote.\n\n"
            "Missions : développement de nouvelles fonctionnalités en Ruby on Rails, écriture de "
            "tests avec RSpec, intégration de vues avec HTML/CSS, participation aux code reviews. "
            "L'intégralité du backend de la plateforme est écrite en Ruby, et la maîtrise de ce "
            "langage est indispensable pour contribuer au code dès l'arrivée.\n\n"
            "Profil recherché : Bac+2/Bac+3 en développement informatique, 0 à 2 ans d'expérience. "
            "Compétences requises : Ruby, Ruby on Rails, HTML, CSS. Aucune expérience en Ruby "
            "n'est acceptée sans formation préalable au langage, car l'équipe ne dispose pas de "
            "temps dédié à l'onboarding technique ce trimestre."
        ),
        "skills": ["Ruby", "HTML", "CSS"],
    },
    {
        "id": "bm16",
        "title": "Ingénieur Réseau Junior – Notions de Programmation",
        "company": "NetLine Infrastructure",
        "location": "Grenoble, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "NetLine Infrastructure conçoit et exploite des réseaux d'entreprise pour des "
            "clients industriels. Nous recrutons un(e) Ingénieur Réseau Junior pour notre site "
            "de Grenoble.\n\n"
            "Missions : configuration et supervision d'équipements réseau (routeurs, switchs, "
            "firewalls), diagnostic d'incidents de connectivité, rédaction de scripts Bash "
            "simples pour automatiser des vérifications de supervision, documentation des "
            "architectures réseau. Le poste est centré sur l'ingénierie réseau ; la "
            "programmation reste secondaire et se limite à quelques scripts d'automatisation "
            "de tâches répétitives.\n\n"
            "Profil recherché : Bac+2 en réseaux et télécommunications. Débutant accepté. "
            "Compétences : réseaux, notions Linux et Bash appréciées."
        ),
        "skills": ["Linux", "Bash"],
    },
    {
        "id": "bm17",
        "title": "Assistant Cybersécurité – Sensibilisation & Audit Junior",
        "company": "GuardLayer Consulting",
        "location": "Toulouse, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "GuardLayer Consulting accompagne des PME dans l'amélioration de leur posture de "
            "cybersécurité. Nous recherchons un(e) Assistant Cybersécurité Junior pour notre "
            "bureau de Toulouse (télétravail 1 jour/semaine).\n\n"
            "Missions : réalisation d'audits de configuration basiques, animation d'ateliers de "
            "sensibilisation au phishing auprès des collaborateurs clients, rédaction de "
            "rapports d'audit synthétiques, veille sur les vulnérabilités courantes publiées. Le "
            "poste est davantage orienté sensibilisation et audit organisationnel que tests "
            "d'intrusion techniques approfondis.\n\n"
            "Profil recherché : Bac+2/Bac+3 en cybersécurité ou informatique. Débutant accepté. "
            "Notions de Linux et de réseaux appréciées. Bon relationnel indispensable pour "
            "animer des ateliers auprès de publics non techniques."
        ),
        "skills": ["Linux"],
    },
    {
        "id": "bm18",
        "title": "Développeur PHP/Symfony Junior – Maintenance Applicative",
        "company": "Legacy Apps Consulting",
        "location": "Nantes, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDD",
        "description": (
            "Legacy Apps Consulting assure la tierce maintenance applicative (TMA) d'applications "
            "PHP/Symfony pour des clients du secteur public. Nous recherchons un(e) Développeur "
            "PHP/Symfony Junior pour un CDD de 12 mois à Nantes.\n\n"
            "Missions : correction de bugs remontés par les utilisateurs, développement de "
            "petites évolutions fonctionnelles, requêtes MySQL, intégration HTML/CSS, "
            "utilisation de Git pour le suivi des correctifs. Le poste est essentiellement de la "
            "maintenance corrective plutôt que du développement de nouvelles fonctionnalités.\n\n"
            "Profil recherché : Bac+2 en informatique, 0 à 2 ans d'expérience. Compétences : PHP, "
            "Symfony, MySQL, HTML, CSS, Git."
        ),
        "skills": ["PHP", "MySQL", "HTML", "CSS", "Git"],
    },
    {
        "id": "bm19",
        "title": "Technicien Helpdesk Bilingue Anglais – Support N1/N2",
        "company": "GlobalDesk Support",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "GlobalDesk Support fournit une assistance informatique multilingue pour des filiales "
            "d'entreprises internationales basées en France. Nous recherchons un(e) Technicien "
            "Helpdesk Bilingue Anglais pour notre plateau parisien.\n\n"
            "Missions : prise en charge des demandes de support niveau 1 et 2 par téléphone et "
            "ticketing en français et en anglais, diagnostic d'incidents matériels et logiciels, "
            "installation de postes sous Linux et Windows, escalade des incidents complexes vers "
            "les équipes systèmes.\n\n"
            "Profil recherché : Bac+2 en informatique. Débutant accepté. Anglais courant "
            "indispensable (échanges quotidiens avec des utilisateurs anglophones). Notions "
            "Linux appréciées. Ce poste est centré sur le support utilisateur, avec très peu de "
            "programmation."
        ),
        "skills": ["Linux"],
    },
    {
        "id": "bm20",
        "title": "Data Engineer Junior – Python & Automatisation de Pipelines",
        "company": "PipeStream Data",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": True,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "PipeStream Data construit des pipelines de données pour des clients du secteur "
            "e-commerce. Nous recherchons un(e) Data Engineer Junior pour notre équipe "
            "parisienne (télétravail 2 jours/semaine).\n\n"
            "Missions : développement et maintenance de scripts Python pour l'automatisation de "
            "pipelines de données, requêtes et optimisation de bases MySQL, mise en place de "
            "tâches planifiées via des scripts Bash, utilisation de Git pour le versioning du "
            "code, documentation des flux de données.\n\n"
            "Profil recherché : Bac+2/Bac+3 en informatique. 0 à 2 ans d'expérience. Compétences : "
            "Python, MySQL, Bash, Git. La connaissance de Docker est un plus apprécié."
        ),
        "skills": ["Python", "MySQL", "Bash", "Git"],
    },

    # ================================================================
    # 21-30: Clearly irrelevant occupations
    # ================================================================
    {
        "id": "bm21",
        "title": "Comptable Senior H/F",
        "company": "Fiduciaire Auvergne Conseil",
        "location": "Clermont-Ferrand, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "Cabinet d'expertise comptable, nous recherchons un(e) Comptable Senior pour gérer "
            "un portefeuille clients diversifié (TPE/PME).\n\n"
            "Missions : tenue et révision comptable, établissement des déclarations de TVA, "
            "préparation des bilans et liasses fiscales, conseil de premier niveau aux clients, "
            "encadrement d'un(e) assistant(e) comptable.\n\n"
            "Profil recherché : Bac+3/Bac+5 en comptabilité (DCG/DSCG), 5 ans d'expérience "
            "minimum en cabinet. Maîtrise d'Excel et d'un logiciel de comptabilité (Sage, Cegid) "
            "indispensable. Rigueur et autonomie exigées."
        ),
        "skills": [],
    },
    {
        "id": "bm22",
        "title": "Représentant Commercial B2B – Secteur Industrie",
        "company": "IndusPro Distribution",
        "location": "Reims, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "IndusPro Distribution, grossiste en équipements industriels, recherche un(e) "
            "Représentant Commercial B2B pour développer son portefeuille clients dans la "
            "région Grand Est.\n\n"
            "Missions : prospection téléphonique et terrain, négociation de contrats-cadres, "
            "suivi de la relation client, reporting hebdomadaire de l'activité commerciale sur "
            "notre CRM, participation à des salons professionnels.\n\n"
            "Profil recherché : Bac+2 minimum en commerce, 2 ans d'expérience en vente B2B. "
            "Permis B obligatoire (déplacements fréquents). Excellent sens de la négociation et "
            "goût du terrain."
        ),
        "skills": [],
    },
    {
        "id": "bm23",
        "title": "Chargée de Recrutement IT",
        "company": "TalentBridge RH",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "TalentBridge RH est un cabinet de recrutement spécialisé sur les métiers de "
            "l'informatique. Nous recherchons un(e) Chargée de Recrutement IT pour renforcer "
            "notre équipe parisienne.\n\n"
            "Missions : sourcing de candidats développeurs et ingénieurs sur les jobboards et "
            "LinkedIn, rédaction et diffusion d'annonces, conduite d'entretiens de "
            "présélection, suivi du pipeline de candidats dans notre ATS, reporting aux clients "
            "sur l'avancement des recrutements.\n\n"
            "Profil recherché : Bac+3/Bac+5 en ressources humaines ou école de commerce, 2 ans "
            "d'expérience en recrutement, idéalement sur des profils techniques. Un intérêt pour "
            "l'univers IT est un plus mais aucune compétence technique n'est requise pour ce "
            "poste de recruteur."
        ),
        "skills": [],
    },
    {
        "id": "bm24",
        "title": "Marketing Manager – Brand Strategy",
        "company": "Luminate Consumer Goods",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "Luminate Consumer Goods, groupe de biens de grande consommation, recherche un(e) "
            "Marketing Manager pour piloter la stratégie de marque de l'une de ses gammes "
            "phares.\n\n"
            "Missions : définition du plan marketing annuel, pilotage d'études consommateurs, "
            "coordination avec les agences de communication, suivi du P&L de la gamme, "
            "management d'une chargée de marketing junior.\n\n"
            "Profil recherché : Bac+5 école de commerce, 6 ans d'expérience minimum en marketing "
            "grande consommation. Excellentes capacités d'analyse et de présentation. Anglais "
            "courant indispensable."
        ),
        "skills": [],
    },
    {
        "id": "bm25",
        "title": "Talent Acquisition Recruiter – Volume Hiring",
        "company": "Sprint Staffing Group",
        "location": "Lyon, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "Sprint Staffing Group accompagne des entreprises en forte croissance dans leurs "
            "recrutements en volume. Nous recherchons un(e) Talent Acquisition Recruiter pour "
            "notre agence de Lyon.\n\n"
            "Missions : gestion d'un portefeuille de postes à pourvoir en volume, sourcing "
            "multicanal, qualification téléphonique des candidats, organisation de sessions de "
            "recrutement collectif, reporting de l'activité aux clients.\n\n"
            "Profil recherché : Bac+2 minimum, 1 an d'expérience en recrutement ou RH. Aisance "
            "relationnelle et goût du challenge. Aucune compétence technique requise, le poste "
            "couvre tous secteurs d'activité."
        ),
        "skills": [],
    },
    {
        "id": "bm26",
        "title": "Graphiste Freelance – Identité Visuelle",
        "company": "Studio Encre & Pixel",
        "location": "Montpellier, France",
        "countries": ["France"],
        "remote": True,
        "work_mode": "remote",
        "employment_type": "Freelance",
        "description": (
            "Studio Encre & Pixel recherche un(e) Graphiste Freelance pour des missions "
            "récurrentes de création d'identité visuelle pour des clients TPE/PME, en télétravail.\n\n"
            "Missions : conception de logos et chartes graphiques, création de supports de "
            "communication print et digital, retouche photo, présentation des créations aux "
            "clients et intégration des retours.\n\n"
            "Profil recherché : Bac+2/Bac+3 en design graphique ou communication visuelle, "
            "3 ans d'expérience en agence ou freelance. Maîtrise de la suite Adobe Creative "
            "(Illustrator, Photoshop, InDesign) indispensable. Sens artistique et créativité "
            "exigés."
        ),
        "skills": [],
    },
    {
        "id": "bm27",
        "title": "Infirmier(e) Diplômé(e) d'État – Service Urgences",
        "company": "Clinique Sainte-Agnès",
        "location": "Dijon, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "La Clinique Sainte-Agnès recherche un(e) Infirmier(e) Diplômé(e) d'État pour "
            "renforcer son service des urgences.\n\n"
            "Missions : accueil et prise en charge des patients, soins infirmiers courants et "
            "d'urgence, coordination avec l'équipe médicale, tenue des dossiers patients, "
            "participation aux astreintes de nuit et de week-end.\n\n"
            "Profil recherché : Diplôme d'État Infirmier obligatoire, une première expérience en "
            "service d'urgences est un plus. Capacité à gérer le stress et à travailler en "
            "équipe pluridisciplinaire indispensable."
        ),
        "skills": [],
    },
    {
        "id": "bm28",
        "title": "Avocat en Droit des Affaires",
        "company": "Cabinet Lefèvre & Associés",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "Le cabinet Lefèvre & Associés, spécialisé en droit des affaires, recherche un(e) "
            "Avocat Collaborateur pour son bureau parisien.\n\n"
            "Missions : conseil juridique aux clients entreprises, rédaction d'actes et de "
            "contrats commerciaux, gestion de dossiers de fusion-acquisition, représentation des "
            "clients devant les juridictions compétentes, veille juridique sectorielle.\n\n"
            "Profil recherché : Bac+5 (Master 2 en droit des affaires) et CAPA obligatoires, "
            "3 ans d'expérience en cabinet d'avocats. Excellente maîtrise rédactionnelle et "
            "anglais juridique courant requis."
        ),
        "skills": [],
    },
    {
        "id": "bm29",
        "title": "Assistant(e) Administratif(ve) Polyvalent(e)",
        "company": "Cabinet Notarial du Centre",
        "location": "Orléans, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "Cabinet notarial recherche un(e) Assistant(e) Administratif(ve) Polyvalent(e) pour "
            "assurer le bon fonctionnement administratif de l'étude.\n\n"
            "Missions : accueil physique et téléphonique des clients, gestion du courrier et des "
            "agendas, préparation de dossiers administratifs, saisie de documents dans notre "
            "logiciel métier, classement et archivage.\n\n"
            "Profil recherché : Bac+2 en assistanat de direction ou gestion, 1 an d'expérience "
            "souhaité. Maîtrise du Pack Office (Word, Excel, Outlook) indispensable. Sens de "
            "l'organisation et discrétion exigés."
        ),
        "skills": [],
    },
    {
        "id": "bm30",
        "title": "Responsable Commercial Grands Comptes",
        "company": "Meridian Office Solutions",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "Meridian Office Solutions, distributeur de solutions bureautiques, recherche un(e) "
            "Responsable Commercial Grands Comptes pour développer son portefeuille "
            "d'entreprises clientes.\n\n"
            "Missions : gestion et développement d'un portefeuille de grands comptes, "
            "négociation de contrats pluriannuels, animation d'une petite équipe commerciale, "
            "reporting mensuel à la direction commerciale, participation aux salons "
            "professionnels du secteur.\n\n"
            "Profil recherché : Bac+5 école de commerce, 7 ans d'expérience minimum en vente "
            "grands comptes. Leadership et excellent relationnel exigés. Permis B obligatoire."
        ),
        "skills": [],
    },

    # ================================================================
    # 31-40: Keyword traps (technical-sounding, primary role is NOT IT)
    # ================================================================
    {
        "id": "bm31",
        "title": "Sales Engineer – Developer Tools Platform",
        "company": "Vantage DevTools Inc.",
        "location": "Paris, France (hybrid)",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "Vantage DevTools helps engineering teams ship faster with our developer platform, "
            "built on Python and JavaScript. We are hiring a Sales Engineer to run technical "
            "demos and support our sales team during the deal cycle, based in Paris with 2 days "
            "on-site.\n\n"
            "Responsibilities: deliver product demos to prospective customers showing how our "
            "Python and JavaScript SDKs integrate into their stack, answer technical "
            "pre-sales questions, write proof-of-concept integrations for prospects, work "
            "closely with Account Executives to close deals, gather feedback for the product "
            "team.\n\n"
            "Requirements: Bac+5 business or engineering degree, 3 years of experience in a "
            "customer-facing pre-sales or sales engineering role. Comfortable discussing Python "
            "and JavaScript concepts with technical buyers, though you will not be writing "
            "production code. Excellent presentation skills required — this is fundamentally a "
            "sales role, not a development role."
        ),
        "skills": ["Python", "JavaScript"],
    },
    {
        "id": "bm32",
        "title": "Chef de Projet Informatique H/F",
        "company": "Delta Conseil IT",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "Delta Conseil IT recherche un(e) Chef de Projet Informatique pour piloter des "
            "projets de développement pour le compte de clients grands comptes.\n\n"
            "Missions : cadrage des besoins avec les clients, planification et suivi budgétaire "
            "des projets, animation d'équipes de développeurs Python et JavaScript en mode "
            "agile, reporting aux comités de pilotage, gestion des risques projet.\n\n"
            "Profil recherché : Bac+5 en informatique ou école d'ingénieur, 6 ans d'expérience "
            "en gestion de projet informatique. Certification PMP ou Scrum Master appréciée. "
            "Le poste est un poste de management de projet, sans activité de développement "
            "directe."
        ),
        "skills": [],
    },
    {
        "id": "bm33",
        "title": "Product Manager – Technical",
        "company": "Orbital SaaS Co.",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": True,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "Orbital SaaS Co. is looking for a Technical Product Manager to own the roadmap for "
            "our API platform, working closely with our engineering team (Python, Node.js).\n\n"
            "Responsibilities: define product requirements and write detailed specs for the "
            "engineering team, prioritize the backlog, run sprint planning with developers, "
            "analyze usage data to inform roadmap decisions, present roadmap updates to "
            "leadership.\n\n"
            "Requirements: Bac+5 engineering or business degree, 5 years of experience in "
            "product management, ideally on a developer-facing product. Comfortable reading "
            "Python and Node.js code to understand technical constraints, but this role does "
            "not involve writing production code — it is a product ownership role."
        ),
        "skills": [],
    },
    {
        "id": "bm34",
        "title": "Technical Recruiter (Python/DevOps Roles)",
        "company": "CodeMatch Talent",
        "location": "Lyon, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "CodeMatch Talent is a specialist recruitment agency hiring a Technical Recruiter "
            "focused on Python and DevOps positions for our client portfolio in Lyon.\n\n"
            "Responsibilities: source and screen candidates for Python developer and DevOps "
            "engineer roles, write and post job descriptions, conduct first-round phone "
            "interviews to assess technical fit at a high level, manage the candidate pipeline "
            "in our ATS, coordinate interview scheduling with hiring managers.\n\n"
            "Requirements: Bac+3/Bac+5 in HR or business, 2 years of experience in tech "
            "recruitment. Familiarity with Python and DevOps terminology is useful for "
            "screening conversations, but no coding skills are required — this is a recruitment "
            "role, not an engineering role."
        ),
        "skills": [],
    },
    {
        "id": "bm35",
        "title": "Customer Success Manager – SaaS Platform",
        "company": "Northstar CRM Solutions",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": True,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "Northstar CRM Solutions is hiring a Customer Success Manager to own the "
            "post-sales relationship with our enterprise customers using our Python-based "
            "automation platform.\n\n"
            "Responsibilities: onboard new customers onto the platform, run regular check-in "
            "calls, help customers configure integrations (with light guidance, not hands-on "
            "coding), track adoption metrics and flag churn risk, coordinate with support and "
            "product teams on customer issues.\n\n"
            "Requirements: Bac+5 business degree, 4 years of experience in customer success or "
            "account management for a SaaS product. Ability to speak comfortably about Python "
            "and API integrations at a conceptual level. This is a relationship-management "
            "role, not a technical implementation role."
        ),
        "skills": [],
    },
    {
        "id": "bm36",
        "title": "Business Analyst – SQL & Python Reporting",
        "company": "Meridian Business Insights",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "Meridian Business Insights recherche un(e) Business Analyst pour accompagner nos "
            "clients dans l'optimisation de leurs processus métiers, avec des livrables incluant "
            "reporting SQL et Python.\n\n"
            "Missions : conduite d'entretiens avec les métiers pour recueillir les besoins, "
            "rédaction de cahiers des charges fonctionnels, production de reportings à partir "
            "de requêtes SQL et de scripts Python simples, animation d'ateliers de "
            "cartographie de processus, présentation des recommandations à la direction. "
            "L'essentiel du poste porte sur l'analyse de processus métiers et la communication "
            "avec les parties prenantes ; le SQL et le Python ne servent qu'à produire des "
            "livrables ponctuels de reporting.\n\n"
            "Profil recherché : Bac+5 école de commerce ou ingénieur, 3 ans d'expérience en "
            "conseil ou analyse métier. Notions de SQL et Python appréciées."
        ),
        "skills": ["Python"],
    },
    {
        "id": "bm37",
        "title": "Technical Writer – API Documentation",
        "company": "Clarity Docs Studio",
        "location": "Remote (France)",
        "countries": ["France"],
        "remote": True,
        "work_mode": "remote",
        "employment_type": "CDI",
        "description": (
            "Clarity Docs Studio helps SaaS companies produce clear developer documentation. We "
            "are hiring a Technical Writer to own API reference documentation for several "
            "client accounts, fully remote from France.\n\n"
            "Responsibilities: write and maintain API reference guides and integration "
            "tutorials, work with engineering teams to understand new endpoints, produce "
            "onboarding guides for developer portals, review OpenAPI specifications for "
            "accuracy, maintain a consistent documentation style guide.\n\n"
            "Requirements: Bac+3/Bac+5 in communication, technical writing, or a related field, "
            "3 years of experience writing developer-facing documentation for a SaaS or "
            "platform product. Comfortable reading REST API references and SDK guides. "
            "Excellent written English required. This is a writing role — you will not be "
            "asked to write or ship production code."
        ),
        "skills": [],
    },
    {
        "id": "bm38",
        "title": "IT Account Manager – Enterprise Software Sales",
        "company": "Summit Enterprise Solutions",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "Summit Enterprise Solutions distributes enterprise software licenses to large "
            "accounts across France. We are hiring an IT Account Manager to grow our portfolio "
            "of enterprise clients.\n\n"
            "Responsibilities: manage relationships with existing enterprise accounts, identify "
            "upsell opportunities for additional software licenses, coordinate renewal "
            "negotiations, work with pre-sales engineers on technical proposals, report "
            "pipeline status to the sales director.\n\n"
            "Requirements: Bac+5 business school, 5 years of experience in enterprise software "
            "sales or account management. Comfortable discussing IT infrastructure topics with "
            "client CIOs. This is a sales and relationship-management role; no hands-on "
            "technical work is expected."
        ),
        "skills": [],
    },
    {
        "id": "bm39",
        "title": "Cybersecurity Sales Engineer",
        "company": "ShieldVantage Security",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "ShieldVantage Security sells enterprise cybersecurity software to mid-size "
            "companies across Europe. We are hiring a Cybersecurity Sales Engineer to support "
            "our sales team with technical demos and proposals, based in Paris.\n\n"
            "Responsibilities: deliver product demonstrations of our cybersecurity suite to "
            "prospective clients, answer technical questions during the sales cycle, produce "
            "proof-of-value trial configurations, coordinate with the sales team on proposal "
            "content, attend industry trade shows to represent the product.\n\n"
            "Requirements: Bac+5 in cybersecurity, engineering, or business, 4 years of "
            "experience in a pre-sales or sales engineering capacity within cybersecurity. "
            "Strong presentation and negotiation skills required — this role sits within the "
            "sales organization and reports to the VP of Sales, not to engineering."
        ),
        "skills": [],
    },
    {
        "id": "bm40",
        "title": "Solutions Architect – Pre-Sales (Cloud)",
        "company": "Zenith Cloud Partners",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "Zenith Cloud Partners is a reseller of enterprise cloud migration services for "
            "large accounts. We are hiring a Pre-Sales Solutions Architect to support our sales "
            "team with technical proposals for AWS and Azure migrations.\n\n"
            "Responsibilities: design high-level cloud migration proposals for enterprise "
            "prospects, present architecture diagrams to client stakeholders during the sales "
            "cycle, estimate project costs and timelines, coordinate with delivery partners on "
            "proposal handoff, attend client meetings alongside Account Executives.\n\n"
            "Requirements: Bac+5 engineering degree, 6 years of experience in enterprise IT "
            "sales or pre-sales consulting for cloud services. Strong client-facing "
            "communication skills required. This is a commercial pre-sales role focused on "
            "proposal-writing and client presentations, not hands-on cloud engineering."
        ),
        "skills": [],
    },

    # ================================================================
    # 41-50: Hard-constraint traps
    # ================================================================
    {
        "id": "bm41",
        "title": "Développeur Python Junior H/F",
        "company": "Cortex Software Labs",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "Cortex Software Labs recherche un(e) Développeur Python Junior pour rejoindre son "
            "équipe backend à Paris.\n\n"
            "Missions : développement d'API en Python, écriture de requêtes MySQL, tests "
            "unitaires, utilisation de Git, participation aux code reviews.\n\n"
            "Diplôme requis : Bac+5 minimum obligatoire pour ce poste, sans exception possible. "
            "Ce niveau de diplôme est une condition d'éligibilité stricte pour la candidature.\n\n"
            "Autres critères du profil, sans lien avec le diplôme : une première expérience en "
            "développement Python (stage ou alternance) est la bienvenue. Compétences "
            "techniques attendues : Python, MySQL, Git, Linux."
        ),
        "skills": ["Python", "MySQL", "Git", "Linux"],
    },
    {
        "id": "bm42",
        "title": "Senior Backend Developer (Python)",
        "company": "Ironclad Systems",
        "location": "Paris, France (remote possible)",
        "countries": ["France"],
        "remote": True,
        "work_mode": "remote",
        "employment_type": "CDI",
        "description": (
            "Ironclad Systems is hiring a Senior Backend Developer to own our core Python "
            "services, fully remote from France.\n\n"
            "Responsibilities: design and build backend services in Python, mentor junior "
            "engineers, drive architecture decisions, review pull requests, own production "
            "incident response for the backend team.\n\n"
            "Requirements: Bac+3 or equivalent experience, 7 years of professional Python "
            "development experience. Deep expertise in system design, MySQL, Git, and Linux "
            "required. You will be the most senior backend engineer on the team and expected "
            "to lead technical decisions independently."
        ),
        "skills": ["Python", "MySQL", "Git", "Linux"],
    },
    {
        "id": "bm43",
        "title": "Python Developer – 5+ Years Experience Required",
        "company": "Falcon Data Systems",
        "location": "Remote (France)",
        "countries": ["France"],
        "remote": True,
        "work_mode": "remote",
        "employment_type": "CDI",
        "description": (
            "Falcon Data Systems is looking for an experienced Python Developer to join our "
            "remote engineering team.\n\n"
            "Responsibilities: build and maintain data-processing services in Python, write "
            "MySQL queries, collaborate with the platform team on Git-based workflows, "
            "participate in on-call rotation for production incidents.\n\n"
            "Requirements: Bac+2 or equivalent experience accepted. 5+ years of experience "
            "required working professionally with Python in production environments. Strong "
            "MySQL and Git skills expected from day one — this is not an entry-level position "
            "and candidates below the experience threshold will not be considered."
        ),
        "skills": ["Python", "MySQL", "Git"],
    },
    {
        "id": "bm44",
        "title": "Technicien Support IT – Poste sur Site",
        "company": "Atlas Maroc Services Informatiques",
        "location": "Casablanca, Maroc",
        "countries": ["Morocco"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "Atlas Maroc Services Informatiques recherche un(e) Technicien Support IT pour "
            "rejoindre notre équipe basée à Casablanca. Le poste est exclusivement sur site, "
            "aucune option de télétravail depuis l'étranger n'est possible.\n\n"
            "Missions : support utilisateur niveau 1/2, administration de postes sous Linux, "
            "installation de matériel informatique, rédaction de scripts Bash pour la "
            "maintenance courante.\n\n"
            "Profil recherché : Bac+2 en informatique. Débutant accepté. Compétences : Linux, "
            "Bash. Résidence à Casablanca ou capacité à s'y installer indispensable, ce poste "
            "n'est pas éligible au travail à distance."
        ),
        "skills": ["Linux", "Bash"],
    },
    {
        "id": "bm45",
        "title": "Remote Junior Python Developer — US Applicants Only",
        "company": "Cascade Software Inc.",
        "location": "Remote (United States only)",
        "countries": ["United States"],
        "remote": True,
        "work_mode": "remote",
        "employment_type": "CDI",
        "description": (
            "Cascade Software Inc. is hiring a Junior Python Developer for a fully remote "
            "position, open exclusively to candidates authorized to work in the United States. "
            "We are unable to sponsor or support applicants based outside the US due to payroll "
            "and tax constraints.\n\n"
            "Responsibilities: build features in our Python backend, write MySQL queries, use "
            "Git for version control, participate in daily standups (Eastern Time hours).\n\n"
            "Requirements: Bac+2 or equivalent, 0-2 years of experience. Skills: Python, MySQL, "
            "Git, Linux. Must be a US resident with valid US work authorization; this listing "
            "is not open to candidates located in France or elsewhere in Europe."
        ),
        "skills": ["Python", "MySQL", "Git", "Linux"],
    },
    {
        "id": "bm46",
        "title": "Développeur Python Junior – Bac+5 Apprécié",
        "company": "Silverline Software",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "Silverline Software recherche un(e) Développeur Python Junior pour son équipe "
            "produit basée à Paris (télétravail 2 jours/semaine).\n\n"
            "Missions : développement de fonctionnalités backend en Python, requêtes MySQL, "
            "tests unitaires, utilisation de Git, participation aux rituels agiles.\n\n"
            "Profil recherché : Bac+2 en informatique. Un niveau Bac+5 est apprécié mais "
            "absolument pas un critère éliminatoire — nous recrutons avant tout sur les "
            "compétences techniques et la motivation. 0 à 2 ans d'expérience. Compétences : "
            "Python, MySQL, Git, Linux."
        ),
        "skills": ["Python", "MySQL", "Git", "Linux"],
    },
    {
        "id": "bm47",
        "title": "Alternance Développeur Full-Stack – Bac+5 ou Expérience Équivalente",
        "company": "Vertex Web Agency",
        "location": "Lyon, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "Alternance",
        "description": (
            "Vertex Web Agency recherche un(e) alternant(e) Développeur Full-Stack pour "
            "renforcer son équipe technique à Lyon.\n\n"
            "Missions : développement front-end en JavaScript et HTML/CSS, développement "
            "back-end en Node.js, requêtes MySQL, utilisation de Git au quotidien.\n\n"
            "Profil recherché : Bac+5 ou expérience équivalente démontrée par un projet "
            "personnel ou une alternance précédente — les candidatures de profils autodidactes "
            "avec un bon niveau technique sont les bienvenues. Compétences : JavaScript, "
            "Node.js, HTML, CSS, MySQL, Git."
        ),
        "skills": ["JavaScript", "Node.js", "HTML", "CSS", "MySQL", "Git"],
    },
    {
        "id": "bm48",
        "title": "Développeur Python Junior – Équipe Data",
        "company": "Keystone Analytics",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "on_site",
        "employment_type": "CDI",
        "description": (
            "Keystone Analytics recherche un(e) Développeur Python Junior pour rejoindre son "
            "équipe data à Paris.\n\n"
            "Missions : écriture de scripts Python pour le traitement de données, requêtes "
            "MySQL, automatisation de tâches avec Bash, utilisation de Git pour le versioning, "
            "participation aux points d'équipe hebdomadaires.\n\n"
            "Profil du candidat :\n"
            "• Formation : Bac+5\n"
            "• 2 ans d'expérience en développement Python\n"
            "• Compétences : Python, MySQL, Bash, Git\n"
            "• Bon relationnel et esprit d'équipe\n\n"
            "Nous sommes une petite équipe où chacun contribue directement aux décisions "
            "techniques et à la vie de l'entreprise."
        ),
        "skills": ["Python", "MySQL", "Bash", "Git"],
    },
    {
        "id": "bm49",
        "title": "Alternance Développeur Full-Stack – Bac+2 Minimum Validé",
        "company": "Everline Web Studio",
        "location": "Toulouse, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "Alternance",
        "description": (
            "Everline Web Studio recherche un(e) alternant(e) Développeur Full-Stack pour "
            "rejoindre notre équipe technique à Toulouse dans le cadre d'un contrat "
            "d'alternance.\n\n"
            "Missions : développement de fonctionnalités front-end en JavaScript/HTML/CSS, "
            "développement back-end en PHP, requêtes MySQL, utilisation de Git.\n\n"
            "Profil recherché : Bac+2 minimum validé (BTS SIO, DUT Informatique ou équivalent) "
            "requis pour intégrer la formation en alternance visée. Compétences : JavaScript, "
            "PHP, HTML, CSS, MySQL, Git. Débutant accepté, une première expérience (stage) est "
            "un plus non obligatoire."
        ),
        "skills": ["JavaScript", "PHP", "HTML", "CSS", "MySQL", "Git"],
    },
    {
        "id": "bm50",
        "title": "Ingénieur Logiciel Junior H/F",
        "company": "Crimson Systems Engineering",
        "location": "Paris, France",
        "countries": ["France"],
        "remote": False,
        "work_mode": "hybrid",
        "employment_type": "CDI",
        "description": (
            "Crimson Systems Engineering recherche un(e) Ingénieur Logiciel Junior pour "
            "rejoindre son équipe technique à Paris (télétravail 2 jours/semaine).\n\n"
            "Notre stack repose principalement sur Rust : l'ensemble de nos services critiques "
            "est écrit en Rust, et la maîtrise de Rust est indispensable pour contribuer au "
            "code dès votre arrivée. Vous interviendrez sur des modules Rust au quotidien, en "
            "écrivant du code Rust performant, en effectuant des revues de code Rust avec "
            "l'équipe, et en corrigeant des bugs sur nos services Rust existants.\n\n"
            "Missions : développement de nouvelles fonctionnalités sur nos services Rust, "
            "écriture de tests, participation aux code reviews Rust, utilisation de Git pour le "
            "versioning.\n\n"
            "Profil recherché : Bac+2 en informatique, 0 à 2 ans d'expérience. La maîtrise de "
            "Rust est indispensable ; les compétences en Python ou JavaScript ne suffisent pas "
            "pour ce poste centré sur notre stack Rust."
        ),
        "skills": ["Rust", "Git"],
    },
]
