import React from 'react';
import { Flex, Typography } from 'antd';
import { SparkleIcon, useTokens } from '../../components/primitives';
import { defineLayout } from '../layout';

/** For a theme that has a name but nothing to show yet: a message in the middle of the panel. */
function ComingSoonLayout() {
  const token = useTokens();
  return (
    <Flex vertical align="center" justify="center" gap={token.marginXS} style={{ flex: 1, padding: token.paddingLG, textAlign: 'center' }}>
      <SparkleIcon style={{ color: token.colorPrimary, fontSize: token.fontSizeHeading3 }} />
      <Typography.Text strong style={{ fontSize: token.fontSizeHeading4 }}>
        Coming soon
      </Typography.Text>
      <Typography.Text type="secondary">This theme isn't ready yet. Switch back to Alpha from ⋯ → Change theme.</Typography.Text>
    </Flex>
  );
}

export default defineLayout({
  id: 'coming-soon',
  name: 'Coming soon',
  description: 'A placeholder: a "Coming soon" message and nothing else.',
  Component: ComingSoonLayout,
});
