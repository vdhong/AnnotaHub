# AnnotaHub: Vietnamese YouTube Comment Annotation Platform

An open-source platform for automatically collecting Vietnamese YouTube comments and annotating toxic text spans at both sentence-level and token-level.

## 🎯 Objectives

- Collect comments from YouTube videos
- Automatically annotate with custom labels at both sentence level and token/word level using AI
- Create datasets for Vietnamese NLP research
- Support manual label editing and override of AI annotations
- Multi-user collaboration with owner/participant roles

## 🏗️ Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                   Web UI (Django + Bootstrap 5)                 │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐   │
│  │ Projects  │  │ Comments  │  │  Export   │  │  Labels   │   │
│  └───────────┘  └───────────┘  └───────────┘  └───────────┘   │
└────────────────────────────────────────────────────────────────┘
                            │
             ┌──────────────┴──────────────┐
             ▼                             ▼
    ┌──────────────────┐          ┌──────────────────┐
    │  Celery Worker   │          │   PostgreSQL      │
    │  ┌────────────┐  │          │  (Database)       │
    │  │ YouTube    │  │          └──────────────────┘
    │  │ Service    │  │
    │  └────────────┘  │
    │  ┌────────────┐  │
    │  │ Ollama     │  │─→ External AI Service
    │  │ Service    │  │   (Configurable per user)
    │  └────────────┘  │
    └──────────────────┘
             │
             ▼
    ┌──────────────────┐
    │     Redis        │
    │  (Broker)        │
    └──────────────────┘
```

## 📋 Technology Stack

| Component | Technology |
|-----------|------------|
| Framework | Django 4.2 |
| Database | PostgreSQL 15 |
| Task Queue | Celery + Redis |
| YouTube API | YouTube Data API v3 |
| AI Model | Ollama (configurable per user) |
| Container | Docker + Docker Compose |
| Frontend | Bootstrap 5 + Vanilla JS |
| Real-time | Server-Sent Events (SSE) |
| i18n | Django Localization (en, vi) |

## 🚀 Installation & Setup

### Prerequisites

- Docker & Docker Compose installed on your system
- A YouTube Data API v3 key
- Access to an Ollama instance (optional, can be configured per user)

### 1. Clone the repository

```bash
git clone https://gitlab.com/vdhong2008/toxicspan.git
cd ToxiSpan
```

### 2. Copy and configure environment variables

```bash
cp .env.example .env
```

Edit the `.env` file and configure the following essential variables:

- `YOUTUBE_API_KEY` — Your YouTube Data API v3 key for fetching comments
- `OLLAMA_BASE_URL` — URL of your Ollama AI service
- `OLLAMA_API_KEY` — API key for Ollama authentication
- `OLLAMA_MODEL` — Model name for annotation (e.g., `qwen3.6:27b`)
- Database credentials (PostgreSQL)
- Redis connection settings
- Email configuration (SMTP settings for verification emails)
- `SITE_URL` — Base URL of your deployment (for verification/invitation links)

> **Note:** Users can also configure their own API keys and Ollama settings via the User Settings page, which take priority over global settings.

### 3. Build and start with Docker Compose

```bash
docker-compose up -d
```

This starts the following services:
- **web**: Django application server (port 8000)
- **worker**: Celery worker for async tasks
- **db**: PostgreSQL database (port 5432)
- **redis**: Redis message broker (port 6379)

### 4. Create a superuser

```bash
docker-compose exec web python manage.py createsuperuser
```

### 5. Access the application

| Service | URL |
|---------|-----|
| Web UI | http://localhost:8000 |
| Admin Panel | http://localhost:8000/admin |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |
| Health Check | http://localhost:8000/health/ |

## 📱 Usage Guide

### Getting Started

#### Step 1: Register an Account

1. Navigate to http://localhost:8000
2. Click the **Register** link on the login page
3. Fill in the following fields:
   - **Username** (minimum 3 characters, maximum 150)
   - **First Name** and **Last Name**
   - **Email address** (must be unique)
   - **Password** (minimum 8 characters) and confirmation
4. Submit the registration form
5. Check your email inbox for a verification link
6. Click the verification link to activate your account (link expires after 7 days)
7. If you don't receive the email, use the **Resend Email** button on the verification page
8. Once verified, you will be automatically logged in

#### Step 2: Log In

1. Navigate to http://localhost:8000/login
2. Enter your username and password
3. Click **Login**
4. You will be redirected to your Dashboard

#### Step 3: Configure Your User Settings (Recommended)

Before starting annotation work, configure your personal API settings:

1. Click your username in the navigation bar
2. Select **User Settings**
3. Configure the following:
   - **YouTube API Key**: Your personal YouTube Data API v3 key (falls back to global setting if empty)
   - **Ollama Base URL**: Your Ollama service endpoint
   - **Ollama API Key**: Authentication key for Ollama
   - **Ollama Model**: Model name for annotation (e.g., `qwen3.6:27b`)
4. Click **Save**

> **Note:** User settings take priority over global settings. If a user setting is empty, the system falls back to the global configuration.

#### Step 4: Manage Your Labels

Labels are the categories used to annotate comments and tokens:

1. Navigate to the **My Labels** page from the navigation menu
2. Click **Create Label** to add a new label:
   - **Name**: Label name (e.g., "Toxic", "Insult", "Hate Speech")
   - **Description**: When to use this label
   - **Color**: Hex color code for visual display (e.g., `#FF0000`)
3. Edit existing labels by clicking the edit button
4. Delete labels that are not in use (labels currently assigned to comments or tokens cannot be deleted)

#### Step 5: Create a Project

1. From the Dashboard or **Projects** list, click **New Project**
2. Enter a project **name** (must be unique) and optional **description**
3. Click **Create**
4. The project will appear in your "Owned Projects" section

#### Step 6: Configure Project Labels

1. Navigate to your project detail page
2. Go to the **Label Settings** tab
3. Add labels to the project from your owned labels, or create new custom labels
4. Optionally override label name, description, or color for project-specific usage
5. Remove labels that are not needed for this project

#### Step 7: Invite Participants (Optional)

1. Navigate to your project detail page
2. Go to the **Participants** tab (available only to project owners)
3. Click **Invite Member** and enter the email address:
   - If the email belongs to an existing user, they are added as a participant immediately
   - If the email does not exist, an invitation email is sent with a registration link
4. Participants can view and annotate comments but cannot modify project settings or add YouTube links
5. You can remove participants at any time

#### Step 8: Add a YouTube Link

1. Open your project and click **Add YouTube Link**
2. Paste a YouTube video URL
3. The system will automatically:
   - Validate the URL and extract the video ID
   - Fetch video information (title, channel, thumbnail, view count, like count)
   - Display the embedded video
   - Start fetching comments via YouTube API (up to 1000 comments) — **async task**
   - After fetching completes, start AI annotation — **async task**
4. Monitor real-time progress via the progress indicator:
   - **Fetching**: Comments being retrieved from YouTube
   - **Annotating**: AI is labeling comments and tokens
5. Task controls available:
   - **Stop Fetch**: Cancel the current fetch task
   - **Stop Annotate**: Cancel the current annotation task
   - **Continue Annotate**: Resume annotation for unannotated comments
   - **Retry**: Retry a failed fetch task
   - **Clear & Refetch**: Delete existing comments and refetch
   - **Reannotate**: Re-run AI annotation on all comments
6. Once completed, the link status will change to **Fully Annotated**

#### Step 9: View Comments and Tokens

1. Click on a YouTube link to view its detail page
2. Browse through the list of comments with their AI-generated labels
3. Each comment displays:
   - Author information and avatar
   - Original comment text (and source text if translated)
   - AI-assigned label (comment-level)
   - Token-level annotations with highlighted spans
   - Meaningful/skipped status
   - Like count and publication date
4. Use pagination to navigate through large comment sets

#### Step 10: Edit Labels Manually

The system uses a **dual-label system**: each comment and token has an AI label and a manual label. The manual label (if set) takes priority for display and export.

**Edit token-level labels:**
1. On the link detail page, find the comment you want to edit
2. Click on individual words (tokens) to open the label selection
3. Select a label from the dropdown (or choose "None" to clear)
4. Changes are saved immediately via AJAX

**Edit comment-level labels:**
1. Use the label dropdown above each comment
2. Select a label to override the AI classification
3. Changes are saved immediately

**Bulk operations:**
- Use **Reannotate** to re-run AI annotation on all comments in a link
- Use **Continue Annotate** to annotate remaining unannotated comments

#### Step 11: Export Dataset

1. Navigate to the **Export** page from your project
2. Configure export options:
   - Select a specific YouTube link or export all links
   - Choose a filter: All Comments, Toxic Only, or Non-Toxic Only
3. Select the export format:
   - **JSON - Sentence Level**: Comment-level labels with comment text
   - **JSON - Token Level**: Token-level labels with BIO-style format
   - **JSON - LLM Training**: Instruction/input/output format for fine-tuning LLMs
   - **XML - CoNLL Format**: XML structure similar to CoNLL corpus format
   - **CSV - Sentence Level**: CSV for sentence-level data analysis
   - **CSV - Token Level**: CSV for token-level data analysis
4. Click **Export** to download the dataset file
5. Export records are tracked in the system

### Working Workflow

```
User adds YouTube URL
        │
        ▼
┌──────────────────┐
│ Validate URL     │──→ Invalid → Show error
│ Extract video_id │
└────────┬─────────┘
         ▼
┌──────────────────┐
│ Get Video Info   │──→ Failed → Mark link as failed
│ (title, channel) │
└────────┬─────────┘
         ▼
┌──────────────────┐
│ Fetch Comments   │ ← Celery Task (async)
│ (YouTube API)    │    + Real-time SSE progress
└────────┬─────────┘
         ▼
┌──────────────────┐
│ Save to DB       │
│ (deduplication)  │
└────────┬─────────┘
         ▼
┌──────────────────┐
│ AI Annotation    │ ← Celery Task (auto-trigger)
│ (Ollama API)     │    + Comment-level label
└────────┬─────────┘           + Token-level spans
         ▼
┌──────────────────┐
│ Update Progress  │ → SSE → Real-time UI update
│ Notify Complete  │
└──────────────────┘
```

### Admin Panel

1. Navigate to http://localhost:8000/admin
2. Log in with superuser credentials
3. Manage:
   - **Users**: User accounts, groups, permissions
   - **Projects**: Projects, participants, YouTube links
   - **Labels**: Labels, project-label assignments
   - **Comments**: Comments, tokens, annotations
   - **System**: Email verifications, user invitations, task progress, export records, user settings

### Database Backup & Restore

The system provides management commands for database backup and restore:

```bash
# Backup database
docker-compose exec web python manage.py backup_annotahub

# Restore database
docker-compose exec web python manage.py restore_annotahub <backup_file>
```

Backup files are stored in the `backups/` directory.

## 📁 Project Structure

```
ToxiSpan/
├── docker-compose.yml              # Docker services configuration
├── Dockerfile                      # Python application image
├── entrypoint.sh                   # Container entrypoint script
├── requirements.txt                # Python dependencies
├── manage.py                       # Django CLI entry point
├── .env.example                    # Environment variables template
├── annotahub/                      # Django project settings
│   ├── __init__.py                 # Celery auto-discovery import
│   ├── celery.py                   # Celery application configuration
│   ├── settings.py                 # Django settings (DB, email, i18n, etc.)
│   ├── urls.py                     # Root URL configuration
│   └── wsgi.py                     # WSGI application entry
├── comments/                       # Main Django application
│   ├── models.py                   # Data models (Project, YouTubeLink, Comment, Token, etc.)
│   ├── views.py                    # Web views (HTML rendering, form handling)
│   ├── api_views.py                # REST API views (JSON responses)
│   ├── urls.py                     # App URL routing (web + API)
│   ├── tasks.py                    # Celery async tasks (fetch, annotate)
│   ├── admin.py                    # Django admin configuration
│   ├── export_service.py           # Dataset export generators
│   ├── tests.py                    # Test cases
│   ├── apps.py                     # App configuration
│   ├── services/                   # Business logic services
│   │   ├── __init__.py
│   │   ├── email_verification_service.py  # Email verification sending
│   │   ├── invitation_service.py          # User invitation sending
│   │   ├── ollama_service.py              # Ollama AI client
│   │   └── youtube_service.py             # YouTube API client
│   ├── migrations/                 # Database migrations
│   └── management/                 # Custom management commands
│       └── commands/
│           ├── backup_annotahub.py   # Database backup command
│           └── restore_annotahub.py  # Database restore command
├── templates/comments/             # HTML templates (Bootstrap 5)
│   ├── base.html                   # Base template with navigation
│   ├── dashboard.html              # Project dashboard
│   ├── login.html                  # Login page
│   ├── register.html               # Registration page
│   ├── verification_sent.html      # Email verification confirmation
│   ├── accept_invitation.html      # Invitation acceptance form
│   ├── project_list.html           # Projects listing
│   ├── project_form.html           # Project create/edit form
│   ├── project_detail.html         # Project detail with links
│   ├── project_participants.html   # Participant management
│   ├── project_labels_settings.html # Label assignment for projects
│   ├── export.html                 # Dataset export page
│   ├── link_detail.html            # YouTube link detail with comments
│   ├── label_list.html             # User's label listing
│   ├── label_form.html             # Label create/edit form
│   └── user_settings.html          # Per-user API settings
├── static/                         # Static assets
│   ├── css/style.css               # Custom CSS styles
│   └── js/main.js                  # JavaScript (AJAX, SSE, UI logic)
├── locale/                         # Internationalization
│   ├── en/LC_MESSAGES/django.{po,mo}  # English translations
│   └── vi/LC_MESSAGES/django.{po,mo}  # Vietnamese translations
└── backups/                        # Database backup storage
```

## 🔌 API Endpoints

### Web Views (HTML)

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| GET | `/login/` | Login page | Public |
| POST | `/login/` | Submit login | Public |
| GET | `/logout/` | Logout | Authenticated |
| GET | `/register/` | Registration page | Public |
| POST | `/register/` | Submit registration | Public |
| GET | `/verify-email/<token>/` | Email verification | Public |
| POST | `/resend-verification/` | Resend verification email | Public |
| GET | `/invite/<token>/` | Accept invitation | Public |
| GET | `/projects/` | Project list | Authenticated |
| GET/POST | `/projects/create/` | Create project | Authenticated |
| GET | `/projects/<id>/` | Project detail | Authenticated |
| GET/POST | `/projects/<id>/edit/` | Edit project | Owner only |
| POST | `/projects/<id>/delete/` | Delete project | Owner only |
| GET/POST | `/projects/<id>/export/` | Export dataset | Authenticated |
| GET/POST | `/projects/<id>/labels/` | Project label settings | Owner only |
| GET/POST | `/projects/<id>/participants/` | Manage participants | Owner only |
| GET | `/labels/` | My labels list | Authenticated |
| GET/POST | `/labels/create/` | Create label | Authenticated |
| GET/POST | `/labels/<id>/edit/` | Edit label | Owner only |
| POST | `/labels/<id>/delete/` | Delete label | Owner only |
| GET/POST | `/settings/` | User settings | Authenticated |
| GET/POST | `.../links/add/` | Add YouTube link | Authenticated |
| GET | `.../links/<id>/detail/` | Link detail (comments) | Authenticated |
| POST | `.../comments/<id>/set-token-labels/<pos>/` | Set token label | Authenticated |
| POST | `.../comments/<id>/set-comment-labels/` | Set comment label | Authenticated |
| GET | `.../sse/progress/<id>/` | SSE progress stream | Authenticated |
| GET | `/health/` | Health check | Public |

### REST API (JSON)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/projects/` | List all projects |
| POST | `/api/projects/create/` | Create project |
| GET | `/api/projects/<id>/` | Get project detail |
| POST | `/api/projects/<id>/links/` | Add/manage YouTube link |
| GET | `/api/projects/<id>/labels/` | Get project labels |
| GET | `/api/links/<id>/status/` | Get task progress |
| GET | `/api/links/<id>/comments/` | List comments |
| POST | `/api/links/<id>/export/` | Export dataset |
| GET | `/api/comments/<id>/tokens/` | Get comment tokens |
| POST | `/api/comments/<id>/toggle-token/<pos>/` | Toggle token toxicity |
| POST | `/api/comments/<id>/set-token-labels/<pos>/` | Set token labels |
| POST | `/api/comments/<id>/set-comment-labels/` | Set comment labels |
| POST | `/api/comments/<id>/manual-label/` | Manual label override |
| POST | `/api/links/<id>/stop-fetch/` | Stop fetch task |
| POST | `/api/links/<id>/stop-annotate/` | Stop annotation task |
| POST | `/api/links/<id>/retry-fetch/` | Retry failed fetch |
| POST | `/api/links/<id>/clear-refetch/` | Clear and refetch |
| POST | `/api/links/<id>/continue-annotate/` | Continue annotation |
| POST | `/api/links/<id>/reannotate/` | Reannotate all |
| GET | `/api/labels/` | List labels |
| POST | `/api/labels/create/` | Create label |

## 📊 Data Models

### Core Models

| Model | Description |
|-------|-------------|
| **Project** | Organizes YouTube link collections; has an owner and participants |
| **YouTubeLink** | Stores YouTube video info linked to a project |
| **Comment** | Individual YouTube comment with dual-label annotation |
| **Token** | Individual word within a comment with dual-label annotation |
| **Label** | User-owned annotation category (name, color, description) |
| **ProjectLabel** | Links a Label to a Project with optional overrides |

### System Models

| Model | Description |
|-------|-------------|
| **EmailVerification** | One-time tokens for user email verification |
| **UserInvitation** | Invitation tokens for adding users to projects |
| **UserSettings** | Per-user API configuration (YouTube key, Ollama settings) |
| **TaskProgress** | Async task tracking (fetch/annotate progress) |
| **ExportRecord** | Export history tracking |

### Dual-Label System

Each **Comment** and **Token** supports two labels:
- **AI Label**: Automatically assigned by the Ollama model
- **Manual Label**: User-assigned override (takes priority for display and export)

Effective label = `manual_label` if set, otherwise `ai_label`

## ⚙️ Additional Features

- ✅ User authentication with email verification
- ✅ User invitation system for project collaboration
- ✅ Dual-role permission system (Owner: full access, Participant: label-only)
- ✅ Per-user API key and Ollama configuration
- ✅ User-owned label management with project assignment
- ✅ Dual-label system (AI + Manual) for both comments and tokens
- ✅ Project-specific label overrides (name, description, color)
- ✅ Real-time progress tracking via Server-Sent Events
- ✅ Full task control (stop, retry, clear-refetch, continue, reannotate)
- ✅ Comment deduplication
- ✅ Pagination for large datasets
- ✅ Non-Vietnamese comment support (original text preservation)
- ✅ Meaningful/skipped comment filtering
- ✅ Multiple export formats (6 formats)
- ✅ Complete RESTful API
- ✅ Database backup and restore commands
- ✅ Multi-language support (English & Vietnamese)
- ✅ System health monitoring endpoint

## 📄 License

MIT License

## 👥 Contributing

Contributions are welcome! Please open an issue or pull request on [GitLab](https://gitlab.com/vdhong2008/toxicspan).

## 📧 Contact

For questions, please open an issue on [GitLab](https://gitlab.com/vdhong2008/toxicspan).