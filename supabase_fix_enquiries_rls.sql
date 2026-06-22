-- ─── Fix: Enquiries RLS policy for anon key ───────────────────────────────────
-- The enquiries table was created with RLS only for service_role.
-- The Python backend uses the anon key, which was blocked.
-- Run this in Supabase Dashboard → SQL Editor → New query → Run

-- Allow anon key full access (used by the Python backend with the anon public key)
CREATE POLICY "anon_all" ON public.enquiries
    FOR ALL
    TO anon
    USING (true)
    WITH CHECK (true);

-- Also allow authenticated role (for future use)
CREATE POLICY "authenticated_all" ON public.enquiries
    FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);

-- Verify the table is accessible
SELECT COUNT(*) AS enquiry_count FROM public.enquiries;
