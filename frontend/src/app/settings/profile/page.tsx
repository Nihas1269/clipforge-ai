import type { Metadata } from "next";

import { Container } from "@/components/container";
import { PasswordForm } from "@/components/profile/password-form";
import { ProfileForm } from "@/components/profile/profile-form";
import { PRODUCT_NAME } from "@/lib/site";

export const metadata: Metadata = {
  title: `Profile — ${PRODUCT_NAME}`,
};

export default function ProfilePage() {
  return (
    <Container className="py-16 sm:py-24">
      <div className="mx-auto flex max-w-md flex-col gap-12">
        <h1 className="text-3xl font-semibold tracking-tight text-fg">Profile</h1>
        <ProfileForm />
        <PasswordForm />
      </div>
    </Container>
  );
}
