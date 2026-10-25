import nodemailer from "nodemailer";

// In dev this points at Mailpit (http://localhost:8025). In prod, any SMTP provider.
const transport = nodemailer.createTransport(process.env.SMTP_URL ?? "smtp://localhost:1025");

export async function sendMail(opts: { to: string; subject: string; text: string; html?: string }) {
  await transport.sendMail({
    from: process.env.EMAIL_FROM ?? "Tacit <no-reply@tacit.local>",
    ...opts,
  });
}
