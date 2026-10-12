# WeLib

WeLib is a Flask-based digital library for books and manga. The backend uses SQLAlchemy, JWT authentication, bcrypt password hashing, and optional S3-compatible object storage (configured here for Backblaze B2).

## Configuration

Create a local `.env` file or set environment variables in your deployment platform. Start with `.env.example`.

Required:
- `JWT_SECRET`: a long, random secret used to sign access tokens.

For administrator and worker seed accounts:
- `ADMIN_PASSWORD` and `WORKER_PASSWORD` must be set explicitly before those accounts are created.
- Do not use a password from a README or commit one to Git.
- If you change these variables after an account has already been seeded, the existing account password is not automatically changed.

For cloud file storage, configure the appropriate `B2_KEY_ID`, `B2_APP_KEY`, `B2_BUCKET_NAME`, and `B2_ENDPOINT` values. Keep credentials in deployment secrets, never in source control.

Optional:
- `DATABASE_URL`: database connection string; defaults to a local SQLite database.
- `ADMIN_EMAIL`, `WORKER_EMAIL`: account email addresses.
- `CASHAPP_HANDLE`: payment/contact display value used by the app.

## Run locally

1. Install Python 3.11 or a version supported by the dependencies.
2. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   # Windows: .venv\\Scripts\\activate
   # macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Set the environment variables described above.
4. Start with the project's configured server command. For production, use the included Dockerfile and a persistent database/storage configuration.

## Security and deployment checklist

- Use HTTPS in production and a unique `JWT_SECRET`.
- Configure explicit, strong passwords before provisioning privileged accounts.
- Do not expose database files, object-storage credentials, or uploaded PDFs publicly.
- Review access controls for admin/worker endpoints before deployment.
- Keep backups and test restoring them.
- Do not upload copyrighted books unless you have permission to distribute them.

## License

See [LICENSE](LICENSE).
